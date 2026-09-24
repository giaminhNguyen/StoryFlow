"""Dispatcher tests: allow-list, role compatibility, ranking, capacity, quota/rate
failover, waiting_capacity resume, attempts semantics, and active_count safety.

Covers spec M.1-M.20. Deterministic: injected `now`, pre-scored FakeRunner queues,
role_preferences used where ranking must be pinned.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from storyflow import queue
from storyflow.agents import CRASH, FakeRunner, RunnerRegistry
from storyflow.dispatcher import Dispatcher, DispatchOutcome
from storyflow.models import PipelineJob, RunnerAttempt, RunnerInstance, RunnerState, WorkflowSession
from storyflow.protocol import ResultCode, RunnerResult


BASE = datetime(2026, 1, 2, 12, 0, 0)

def at(seconds: int):
    return BASE + timedelta(seconds=seconds)


def make_session(db, *, policy="pause_auto_resume", prefs=None):
    s = WorkflowSession(mode="auto", status="active",
                        all_agents_unavailable_policy=policy, role_preferences=prefs or {})
    db.add(s)
    db.commit()
    return db.get(WorkflowSession, s.id)


def add_runner(db, session, runner_type="fake", *, roles=None, max_concurrency=2,
               enabled=True, state="ready", quota_reset_at=None, cooldown_until=None):
    r = RunnerInstance(
        workflow_session_id=session.id, runner_type=runner_type, enabled=enabled,
        max_concurrency=max_concurrency, state=state,
        supported_roles=roles or ["general_worker"],
        quota_reset_at=quota_reset_at, cooldown_until=cooldown_until,
    )
    db.add(r)
    db.commit()
    return db.get(RunnerInstance, r.id)


def registry_of(dbpairs):
    reg = RunnerRegistry()
    for agent, instance in dbpairs:
        reg.register(instance.id, agent)
    return reg


def enqueue(db, session, *, role="general_worker", payload=None, now=None, **kw):
    return queue.enqueue_job(db, kind="chunk", payload=payload or {}, role=role,
                             session_id=session.id, now=now or BASE, **kw)


def reload(db, job_id):
    return db.scalar(select(PipelineJob).where(PipelineJob.id == job_id)
                     .execution_options(populate_existing=True))


def runner_state(db, runner_id):
    return db.get(RunnerInstance, runner_id, populate_existing=True)


def open_attempt(db, job_id):
    return queue.get_open_attempt(db, job_id)


def attempts(db, job_id):
    return db.scalars(select(RunnerAttempt).where(RunnerAttempt.pipeline_job_id == job_id)
                      .order_by(RunnerAttempt.started_at).execution_options(populate_existing=True)).all()


# --- allow-list / roles ----------------------------------------------------


def test_unselected_runner_never_receives_job(db):
    session = make_session(db)
    other = make_session(db)
    in_session = add_runner(db, session, "fake_a")
    outsider = add_runner(db, other, "fake_b")
    reg = registry_of([(FakeRunner("fake_a"), in_session), (FakeRunner("fake_b"), outsider)])
    disp = Dispatcher(reg)

    enqueue(db, session, now=at(0))
    outcome, _ = disp.run_round(db, session, now=at(0))

    assert outcome == DispatchOutcome.DISPATCHED_SUCCESS
    assert reg.get(outsider.id).invocations == [], "runner of another session must never be selected"


def test_role_compatibility_filters_runners(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a", roles=["general_worker"])
    b = add_runner(db, session, "fake_b", roles=["story_writer"])
    reg = registry_of([(FakeRunner("fake_a"), a), (FakeRunner("fake_b"), b)])
    disp = Dispatcher(reg)

    enqueue(db, session, role="story_writer", now=at(0))
    disp.run_round(db, session, now=at(0))

    assert reg.get(a.id).invocations == [], "wrong-role runner must not be selected"
    assert len(reg.get(b.id).invocations) == 1
    assert reg.get(b.id).invocations[0].role == "story_writer"
    assert attempts(db, reg.get(b.id).invocations[0].job_id)[0].role == "story_writer"


def test_role_preference_ranks_before_load(db):
    session = make_session(db, prefs={"story_writer": ["preferred"]})
    pref = add_runner(db, session, "preferred", roles=["story_writer"], max_concurrency=2)
    other = add_runner(db, session, "other", roles=["story_writer"], max_concurrency=2)
    reg = registry_of([(FakeRunner("preferred"), pref), (FakeRunner("other"), other)])
    disp = Dispatcher(reg)

    # occupy one slot on the PREFERRED runner: load 0.5 vs 0.0 for `other`
    loader = enqueue(db, session, role="story_writer", now=at(0))
    queue.claim_next_job(db, "w-load", 60, runner=pref, now=at(0))
    assert runner_state(db, pref.id).active_count == 1
    assert reload(db, loader.id).status == "processing"

    job = enqueue(db, session, role="story_writer", now=at(1))
    disp.run_round(db, session, now=at(1))

    assert reg.get(other.id).invocations == []
    assert len(reg.get(pref.id).invocations) == 1
    assert reload(db, job.id).status == "completed"


# --- ranking / capacity ----------------------------------------------------


def test_least_loaded_runner_wins(db):
    session = make_session(db)
    busy = add_runner(db, session, "fake_busy", max_concurrency=2)
    free = add_runner(db, session, "fake_free", max_concurrency=2)
    reg = registry_of([(FakeRunner("fake_busy"), busy), (FakeRunner("fake_free"), free)])
    disp = Dispatcher(reg)

    # occupy one slot on `busy` (simulate an in-flight dispatch elsewhere)
    loader = enqueue(db, session, now=at(0))
    queue.claim_next_job(db, "w-load", 60, runner=busy, now=at(0))
    assert runner_state(db, busy.id).active_count == 1
    assert reload(db, loader.id).status == "processing"

    enqueue(db, session, now=at(1))
    disp.run_round(db, session, now=at(1))

    assert len(reg.get(free.id).invocations) == 1, "free runner must win over a loaded one"
    assert reg.get(busy.id).invocations == []


def test_max_concurrency_excludes_full_runner(db):
    session = make_session(db)
    only = add_runner(db, session, "fake_full", max_concurrency=1)
    reg = registry_of([(FakeRunner("fake_full"), only)])
    disp = Dispatcher(reg)

    loader = enqueue(db, session, now=at(0))
    queue.claim_next_job(db, "w-load", 60, runner=only, now=at(0))
    assert runner_state(db, only.id).active_count == 1  # max_concurrency=1 -> full
    job = enqueue(db, session, now=at(1))

    outcome, _ = disp.run_round(db, session, now=at(1))

    assert outcome == DispatchOutcome.PARKED_NO_CANDIDATE
    assert reg.get(only.id).invocations == []
    assert reload(db, job.id).status == "waiting_capacity"


# --- quota semantics -------------------------------------------------------


def test_quota_releases_capacity_and_parks_runner(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a", max_concurrency=2)
    reg = registry_of([(FakeRunner("fake_a", results=[ResultCode.QUOTA_EXHAUSTED]), a)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    outcome = disp.dispatch_job(db, session, reload(db, job.id), at(0))

    assert outcome == DispatchOutcome.DISPATCHED_QUOTA
    assert reload(db, job.id).status == "queued"
    r = runner_state(db, a.id)
    assert r.active_count == 0, "quota must release the capacity slot"
    assert r.state == RunnerState.QUOTA_EXHAUSTED.value
    assert r.quota_reset_at == at(0) + timedelta(seconds=disp.quota_reset_seconds)


def test_quota_does_not_increment_business_attempts(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a", results=[ResultCode.QUOTA_EXHAUSTED]), a)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0), max_attempts=3)
    disp.dispatch_job(db, session, reload(db, job.id), at(0))

    row = reload(db, job.id)
    assert row.attempts == 0
    assert row.infrastructure_failures == 0
    assert row.last_error_code == "quota_exhausted"


def test_quota_failover_goes_to_another_runner(db):
    session = make_session(db, prefs={"general_worker": ["fake_a", "fake_b"]})
    a = add_runner(db, session, "fake_a")
    b = add_runner(db, session, "fake_b")
    reg = registry_of([
        (FakeRunner("fake_a", results=[RunnerResult(
            code=ResultCode.QUOTA_EXHAUSTED, quota_reset_at=at(500))]), a),
        (FakeRunner("fake_b"), b),
    ])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    outcome, _ = disp.run_round(db, session, now=at(0))

    assert outcome == DispatchOutcome.DISPATCHED_SUCCESS
    assert len(reg.get(a.id).invocations) == 1
    assert len(reg.get(b.id).invocations) == 1
    assert reload(db, job.id).status == "completed"


def test_failover_never_reaches_unselected_runner(db):
    session = make_session(db)
    other = make_session(db)
    in_session = add_runner(db, session, "fake_a")
    outsider = add_runner(db, other, "fake_b")
    reg = registry_of([(FakeRunner("fake_a", results=[ResultCode.QUOTA_EXHAUSTED]), in_session),
                       (FakeRunner("fake_b"), outsider)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    outcome, _ = disp.run_round(db, session, now=at(0))

    assert outcome == DispatchOutcome.PARKED_NO_CANDIDATE
    assert reload(db, job.id).status == "waiting_capacity"
    assert reg.get(outsider.id).invocations == []


# --- waiting_capacity / resume ---------------------------------------------


def test_all_exhausted_parks_with_pause_auto_resume(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a", results=[ResultCode.QUOTA_EXHAUSTED]), a)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    outcome, _ = disp.run_round(db, session, now=at(0))

    assert outcome == DispatchOutcome.PARKED_NO_CANDIDATE
    assert reload(db, job.id).status == "waiting_capacity"
    assert reload(db, job.id).attempts == 0


def test_ready_runner_resumes_waiting_job(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a", results=[ResultCode.QUOTA_EXHAUSTED]), a)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    disp.run_round(db, session, now=at(0))
    assert reload(db, job.id).status == "waiting_capacity"

    b = add_runner(db, session, "fake_b")
    reg.register(b.id, FakeRunner("fake_b"))
    outcome, _ = disp.run_round(db, session, now=at(2))

    assert outcome == DispatchOutcome.DISPATCHED_SUCCESS
    assert reload(db, job.id).status == "completed"


def test_rate_limit_cooldown_suspends_job_then_resumes(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a", results=[RunnerResult(
        code=ResultCode.RATE_LIMITED, retry_after=30)]), a)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    disp.dispatch_job(db, session, reload(db, job.id), at(0))

    row = reload(db, job.id)
    assert row.status == "queued"
    assert row.scheduled_at == at(30)
    r = runner_state(db, a.id)
    assert r.state == RunnerState.COOLDOWN.value
    assert r.cooldown_until == at(30)
    assert r.active_count == 0

    job2 = enqueue(db, session, now=at(1))
    outcome, _ = disp.run_round(db, session, now=at(1))
    assert outcome == DispatchOutcome.PARKED_NO_CANDIDATE, "cooling runner must not take work"
    assert reload(db, job2.id).status == "waiting_capacity"

    outcome2, _ = disp.run_round(db, session, now=at(31))
    assert outcome2 is DispatchOutcome.DISPATCHED_SUCCESS or reload(db, job2.id).status == "completed"


# --- attempt lifecycle / active_count safety -------------------------------


def test_attempt_created_at_dispatch(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a"), a)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    disp.run_round(db, session, now=at(0))

    recs = attempts(db, job.id)
    assert len(recs) == 1
    assert recs[0].result_type == "success"
    assert recs[0].runner_instance_id == a.id
    assert recs[0].attempt_number == reload(db, job.id).execution_count


def test_success_closes_attempt_once(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a"), a)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    disp.run_round(db, session, now=at(0))
    assert open_attempt(db, job.id) is None

    # extra finalize attempts can neither create a second attempt nor decrement twice
    row = reload(db, job.id)
    with pytest.raises(queue.LostOwnership):
        queue.complete_job(db, job.id, a.id, row.claim_token or "stale", outcome="success", now=at(1))
    assert runner_state(db, a.id).active_count == 0
    assert attempts(db, job.id)[-1].result_type == "success"


def test_repeated_stale_recovery_no_double_decrement(session_factory):
    s = session_factory()
    session = make_session(s)
    a = add_runner(s, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a"), a)])
    disp = Dispatcher(reg)

    job = enqueue(s, session, now=at(0))
    queue.claim_next_job(s, "w-dispatch", 60, runner=a, now=at(0))   # in-flight dispatch
    assert runner_state(s, a.id).active_count == 1

    assert queue.recover_stale_jobs(s, now=at(61)) == 1
    assert runner_state(s, a.id).active_count == 0
    assert attempts(s, job.id)[-1].result_type == "stale"

    assert queue.recover_stale_jobs(s, now=at(62)) == 0
    assert runner_state(s, a.id).active_count == 0
    assert reload(s, job.id).attempts == 0
    assert reload(s, job.id).infrastructure_failures == 1
    s.close()


def test_active_count_never_negative(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a"), a)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    disp.run_round(db, session, now=at(0))
    assert runner_state(db, a.id).active_count == 0

    for _ in range(3):  # everything after the real close is a no-op
        queue.recover_stale_jobs(db, now=at(61))
        row = reload(db, job.id)
        with pytest.raises(queue.LostOwnership):
            queue.complete_job(db, job.id, a.id, row.claim_token or "t", outcome="success", now=at(62))

    assert runner_state(db, a.id).active_count >= 0
    assert runner_state(db, a.id).active_count == 0


def test_crash_is_infra_not_business(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a", results=[CRASH]), a)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    outcome, _ = disp.run_round(db, session, now=at(0))

    assert outcome == DispatchOutcome.DISPATCHED_REQUEUED
    row = reload(db, job.id)
    assert row.status == "queued"
    assert row.attempts == 0
    assert row.infrastructure_failures == 1
    assert attempts(db, job.id)[-1].result_type == "runner_crashed"
    assert runner_state(db, a.id).active_count == 0


def test_task_failed_is_business(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a", results=[ResultCode.TASK_FAILED]), a)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0), max_attempts=3)
    outcome, _ = disp.run_round(db, session, now=at(0))

    assert outcome == DispatchOutcome.DISPATCHED_REQUEUED
    row = reload(db, job.id)
    assert row.attempts == 1
    assert row.infrastructure_failures == 0
    assert attempts(db, job.id)[-1].result_type == "task_failed"


def test_invalid_output_increments_business_attempt(db):
    session = make_session(db)
    a = add_runner(db, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a", results=[ResultCode.INVALID_OUTPUT]), a)])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0), max_attempts=3)
    outcome, _ = disp.run_round(db, session, now=at(0))

    assert outcome == DispatchOutcome.DISPATCHED_REQUEUED
    assert reload(db, job.id).attempts == 1
    assert reload(db, job.id).status == "queued"


# --- ownership across failover / late results ------------------------------


def test_quota_failover_preserves_ownership(db):
    session = make_session(db, prefs={"general_worker": ["fake_a", "fake_b"]})
    a = add_runner(db, session, "fake_a")
    b = add_runner(db, session, "fake_b")
    reg = registry_of([
        (FakeRunner("fake_a", results=[RunnerResult(
            code=ResultCode.QUOTA_EXHAUSTED, quota_reset_at=at(500))]), a),
        (FakeRunner("fake_b"), b),
    ])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    disp.run_round(db, session, now=at(0))

    recs = attempts(db, job.id)
    assert [r.result_type for r in recs] == ["quota_exhausted", "success"]
    assert recs[0].runner_instance_id == a.id
    assert recs[1].runner_instance_id == b.id
    assert reload(db, job.id).status == "completed"
    assert reload(db, job.id).worker_id is None


def test_quota_failover_did_not_increment_attempts(db):
    session = make_session(db, prefs={"general_worker": ["fake_a", "fake_b"]})
    a = add_runner(db, session, "fake_a")
    b = add_runner(db, session, "fake_b")
    reg = registry_of([
        (FakeRunner("fake_a", results=[ResultCode.QUOTA_EXHAUSTED]), a),
        (FakeRunner("fake_b"), b),
    ])
    disp = Dispatcher(reg)

    job = enqueue(db, session, now=at(0))
    disp.run_round(db, session, now=at(0))
    assert reload(db, job.id).attempts == 0
    assert reload(db, job.id).execution_count == 2, "quota failover = second dispatch, still zero business attempts"


def test_late_stale_result_cannot_finalize(session_factory):
    s = session_factory()
    session = make_session(s)
    a = add_runner(s, session, "fake_a")
    reg = registry_of([(FakeRunner("fake_a"), a)])
    disp = Dispatcher(reg)

    job = enqueue(s, session, now=at(0))
    stale_claim = queue.claim_next_job(s, a.id, 60, runner=a, now=at(0))  # in-flight on A
    stale_token = stale_claim.claim_token
    assert stale_claim.claim_token is not None

    assert queue.recover_stale_jobs(s, now=at(61)) == 1  # expired: A's slot freed, job requeued
    assert reload(s, job.id).status == "queued"
    assert runner_state(s, a.id).active_count == 0

    b2 = add_runner(s, session, "fake_b2")
    reg.register(b2.id, FakeRunner("fake_b2"))
    disp.run_round(s, session, now=at(62))  # new owner dispatches on b2

    # the stale runner A "returns late" with its old token: must be rejected
    with pytest.raises(queue.LostOwnership):
        queue.complete_job(s, job.id, a.id, stale_token, outcome="success", now=at(63))
    with pytest.raises(queue.LostOwnership):
        queue.fail_job(s, job.id, a.id, stale_token, error_code="boom", now=at(63))

    row = reload(s, job.id)
    assert row.status == "completed"
    assert row.outcome == "success"
    assert row.worker_id is None
    assert row.last_error_code != "boom"
    s.close()