"""Queue service tests: lifecycle, dedupe, atomic claim, ownership, leases, stale recovery.

Phase 2 semantics: PipelineJob.attempts counts business failures only;
execution_count counts dispatches; capacity is tied to open RunnerAttempt close.
Deterministic: `now` is injected, concurrency uses a threading.Barrier.
"""

import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from storyflow import queue
from storyflow.models import (
    PipelineJob,
    RunnerAttempt,
    RunnerInstance,
    RunnerState,
    WorkflowSession,
)

BASE = datetime(2026, 1, 1, 12, 0, 0)


def at(seconds: int) -> datetime:
    return BASE + timedelta(seconds=seconds)


def add_job(db, kind="test", **kw):
    defaults = dict(scheduled_at=BASE, priority=0, max_attempts=5, max_infra_attempts=5)
    defaults.update(kw)
    job = PipelineJob(kind=kind, **defaults)
    db.add(job)
    db.commit()
    return job


def add_runner(db, **kw):
    defaults = dict(runner_type="story", enabled=True, max_concurrency=2, state=RunnerState.READY.value)
    defaults.update(kw)
    runner = RunnerInstance(**defaults)
    db.add(runner)
    db.commit()
    return runner


def claim(db, job_id, worker_id, *, now=BASE, lease=60, runner=None):
    return queue.claim_next_job(db, worker_id, lease, runner=runner, now=now)


def reload(db, job_id):
    return db.scalar(select(PipelineJob).where(PipelineJob.id == job_id).execution_options(populate_existing=True))


def reload_runner(db, runner_id):
    return db.scalar(
        select(RunnerInstance).where(RunnerInstance.id == runner_id).execution_options(populate_existing=True)
    )


def set_status(db, job_id, status):
    db.execute(PipelineJob.__table__.update().where(PipelineJob.id == job_id).values(status=status))
    db.commit()


# --- lifecycle -------------------------------------------------------------


def test_enqueue_job_and_defaults(db):
    job = queue.enqueue_job(db, kind="story_gen", payload={"chapter": 1}, dedupe_key="ch1",
                            priority=5, channel_fairness_key="chan-a", now=BASE)
    assert job.status == "queued"
    assert job.attempts == 0
    assert job.execution_count == 0
    assert job.infrastructure_failures == 0
    assert job.max_attempts == 5
    assert job.role == "general_worker"
    assert job.payload_json == {"chapter": 1}
    assert job.worker_id is None and job.claim_token is None and job.lease_expires_at is None
    assert job.scheduled_at == BASE


def test_active_dedupe_constraint_blocks_only_active_statuses(db):
    first = queue.enqueue_job(db, kind="story_gen", dedupe_key="dup-1", now=BASE)
    second = queue.enqueue_job(db, kind="story_gen", dedupe_key="dup-1", now=BASE)
    assert second.id == first.id
    rows = db.scalar(select(func.count(PipelineJob.id)))
    assert rows == 1

    queued = claim(db, first.id, "w1", now=BASE)
    deduped_during_processing = queue.enqueue_job(db, kind="story_gen", dedupe_key="dup-1", now=BASE)
    assert deduped_during_processing.id == first.id
    assert queued.status == "processing"

    queue.move_to_waiting_capacity(db, first.id, "w1", queued.claim_token, now=at(1))
    deduped_while_waiting = queue.enqueue_job(db, kind="story_gen", dedupe_key="dup-1", now=at(2))
    assert deduped_while_waiting.id == first.id
    assert reload(db, first.id).status == "waiting_capacity"


def test_terminal_jobs_free_dedupe_slot(db):
    # completed and failed both free the dedupe slot
    for i, finalize in enumerate([
        lambda d, claimed: queue.complete_job(d, claimed.id, "w1", claimed.claim_token, outcome="success", now=at(10)),
        lambda d, claimed: queue.fail_job(d, claimed.id, "w1", claimed.claim_token, error_code="ERR", now=at(10)),
    ]):
        job = queue.enqueue_job(db, kind="story_gen", dedupe_key=f"free-{i}", now=BASE)
        claimed = claim(db, job.id, "w1", now=BASE)
        finalize(db, claimed)
        replacement = queue.enqueue_job(db, kind="story_gen", dedupe_key=f"free-{i}", now=at(20))
        assert replacement.id != job.id

    # cancelled (manual terminal state) also frees the slot
    cancelled = queue.enqueue_job(db, kind="story_gen", dedupe_key="free-c", now=BASE)
    set_status(db, cancelled.id, "cancelled")
    replacement = queue.enqueue_job(db, kind="story_gen", dedupe_key="free-c", now=at(1))
    assert replacement.id != cancelled.id


# --- atomic claim ----------------------------------------------------------


def test_two_workers_cannot_both_claim_same_job(session_factory, engine):
    seeder = session_factory()
    job = add_job(seeder, kind="race")
    job_id = job.id
    seeder.close()

    barrier = threading.Barrier(2, timeout=15)
    results = []

    def worker(wid):
        sess = session_factory()
        try:
            barrier.wait()
            claimed = queue.claim_next_job(sess, wid, 60, now=BASE)
            results.append((wid, claimed.id if claimed else None))
        finally:
            sess.close()

    threads = [threading.Thread(target=worker, args=(wid,)) for wid in ("w1", "w2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    winners = [wid for wid, jid in results if jid == job_id]
    assert len(winners) == 1, f"both workers claimed the same job: {results}"
    not_winning = [jid for wid, jid in results if wid not in winners]
    assert all(jid is None for jid in not_winning)

    s = session_factory()
    row = reload(s, job_id)
    assert row.status == "processing"
    assert row.worker_id in winners
    assert row.execution_count == 1
    assert row.attempts == 0
    s.close()


# --- ownership guards ------------------------------------------------------


def test_exact_owner_can_complete(db):
    job = add_job(db)
    claimed = claim(db, job.id, "w1", now=BASE)
    assert claimed.execution_count == 1
    assert claimed.attempts == 0
    assert claimed.worker_id == "w1"
    assert claimed.claim_token and claimed.lease_expires_at == at(60)
    done = queue.complete_job(db, job.id, "w1", claimed.claim_token, outcome="success", now=at(5))
    assert done.status == "completed"
    assert done.finished_at == at(5)
    assert done.outcome == "success"
    assert done.worker_id is None and done.claim_token is None


def test_wrong_worker_cannot_complete(db):
    job = add_job(db)
    claimed = claim(db, job.id, "w1", now=BASE)
    with pytest.raises(queue.LostOwnership):
        queue.complete_job(db, job.id, "w2", claimed.claim_token, outcome="success", now=at(5))
    row = reload(db, job.id)
    assert row.status == "processing"
    assert row.worker_id == "w1"
    assert row.claim_token == claimed.claim_token


def test_wrong_claim_token_cannot_complete(db):
    job = add_job(db)
    claimed = claim(db, job.id, "w1", now=BASE)
    with pytest.raises(queue.LostOwnership):
        queue.complete_job(db, job.id, "w1", "forged-token", outcome="success", now=at(5))
    with pytest.raises(queue.LostOwnership):
        queue.fail_job(db, job.id, "w1", "forged-token", error_code="ERR", now=at(5))
    row = reload(db, job.id)
    assert row.status == "processing"


# --- lease + stale recovery ------------------------------------------------


def test_only_expired_processing_jobs_recovered(session_factory):
    s = session_factory()
    queued = add_job(s, kind="a", scheduled_at=at(100))  # never due: stays queued
    expired = add_job(s, kind="b", scheduled_at=BASE)     # due first
    live = add_job(s, kind="c", scheduled_at=at(1))       # due second
    parked = add_job(s, kind="d", scheduled_at=at(2))     # due third
    s.close()

    s1 = session_factory()
    c_expired = claim(s1, expired.id, "w1", now=BASE)             # lease expires at(60)
    c_live = claim(s1, live.id, "w1", lease=120, now=at(2))       # lease expires at(122): stays live
    c_parked = claim(s1, parked.id, "w1", now=at(3))              # parked
    queue.move_to_waiting_capacity(s1, c_parked.id, "w1", c_parked.claim_token, now=at(4))
    s1.close()

    s2 = session_factory()
    recovered = queue.recover_stale_jobs(s2, now=at(61))  # only `c_expired` lease < now
    assert recovered == 1

    assert reload(s2, c_expired.id).status == "queued"
    assert reload(s2, c_expired.id).last_error_code == "LEASE_EXPIRED"
    assert reload(s2, c_expired.id).infrastructure_failures == 1
    assert reload(s2, c_live.id).status == "processing"
    assert reload(s2, c_parked.id).status == "waiting_capacity"
    assert reload(s2, queued.id).status == "queued"
    s2.close()


def test_active_lease_not_recovered(session_factory):
    s = session_factory()
    live = add_job(s, kind="c")
    s.close()
    s1 = session_factory()
    claim(s1, live.id, "w1", now=BASE)
    s1.close()
    s2 = session_factory()
    assert queue.recover_stale_jobs(s2, now=at(30)) == 0
    assert reload(s2, live.id).status == "processing"
    assert reload(s2, live.id).lease_expires_at == at(60)
    s2.close()


def test_stale_owner_cannot_finalize_after_reclaim(session_factory):
    s = session_factory()
    job = add_job(s, kind="x", max_attempts=3, max_infra_attempts=5)
    s.close()

    s1 = session_factory()
    first = claim(s1, job.id, "w1", now=BASE)
    s1.close()

    s2 = session_factory()
    assert queue.recover_stale_jobs(s2, now=at(61)) == 1  # requeued, business attempts untouched
    assert reload(s2, job.id).status == "queued"
    assert reload(s2, job.id).attempts == 0
    new_owner = claim(s2, job.id, "w2", now=at(62))
    assert new_owner.execution_count == 2
    token2 = new_owner.claim_token
    s2.close()

    s3 = session_factory()
    with pytest.raises(queue.LostOwnership):
        queue.complete_job(s3, job.id, "w1", first.claim_token, outcome="stale", now=at(63))
    row = reload(s3, job.id)
    assert row.status == "processing"
    assert row.worker_id == "w2"
    assert row.claim_token == token2

    done = queue.complete_job(s3, job.id, "w2", token2, outcome="success", now=at(64))
    assert done.status == "completed" and done.outcome == "success"
    s3.close()


def test_late_exception_from_stale_worker_cannot_corrupt_new_owner(session_factory):
    s = session_factory()
    job = add_job(s, kind="x", max_attempts=2, max_infra_attempts=5)
    s.close()

    s1 = session_factory()
    first = claim(s1, job.id, "w1", now=BASE)
    stale_token = first.claim_token
    s1.close()

    # lease expires; infra counter is still under cap -> requeue, then a new owner claims
    s2 = session_factory()
    assert queue.recover_stale_jobs(s2, now=at(61)) == 1
    queue.claim_next_job(s2, "w2", 60, now=at(62))
    s2.close()

    # the stale worker's late "hard failure" must not clobber the new owner's attempt
    s3 = session_factory()
    with pytest.raises(queue.LostOwnership):
        queue.fail_job(s3, job.id, "w1", stale_token, error_code="EXCEPTION", error_message="boom", now=at(63))
    row = reload(s3, job.id)
    assert row.status == "processing"
    assert row.worker_id == "w2"
    assert row.last_error_code == "LEASE_EXPIRED", "recovery diagnostics remain; stale EXCEPTION must not land"
    assert row.last_error_message != "boom"
    with pytest.raises(queue.LostOwnership):
        queue.complete_job(s3, job.id, "w1", stale_token, outcome="corrupted", now=at(63))
    assert reload(s3, job.id).outcome is None
    s3.close()


# --- lease renewal ---------------------------------------------------------


def test_lease_renewal_only_current_owner(db):
    job = add_job(db)
    claimed = claim(db, job.id, "w1", now=BASE)
    token = claimed.claim_token

    assert queue.renew_lease(db, job.id, "w1", token, 60, now=at(30)) is True
    assert reload(db, job.id).lease_expires_at == at(90)

    assert queue.renew_lease(db, job.id, "w2", token, 60, now=at(31)) is False
    assert queue.renew_lease(db, job.id, "w1", "forged", 60, now=at(31)) is False

    queue.complete_job(db, job.id, "w1", token, outcome="success", now=at(40))
    assert queue.renew_lease(db, job.id, "w1", token, 60, now=at(41)) is False


def test_exhausted_stale_job_fails_expiring_via_infra_counter(session_factory):
    s = session_factory()
    job = add_job(s, kind="x", max_attempts=5, max_infra_attempts=1)  # 1 infra failure kills it
    s.close()
    s1 = session_factory()
    first = claim(s1, job.id, "w1", now=BASE)
    s1.close()
    s2 = session_factory()
    assert queue.recover_stale_jobs(s2, now=at(61)) == 1
    row = reload(s2, job.id)
    assert row.status == "failed"
    assert row.outcome == "failed"
    assert row.finished_at == at(61)
    assert row.attempts == 0, "lease expiry is infrastructure, not a business attempt"
    assert row.infrastructure_failures == 1
    with pytest.raises(queue.LostOwnership):
        queue.complete_job(s2, job.id, "w1", first.claim_token, outcome="success", now=at(62))
    assert reload(s2, job.id).status == "failed"
    attempts = s2.scalars(select(RunnerAttempt).where(RunnerAttempt.pipeline_job_id == job.id)).all()
    assert attempts and attempts[-1].result_type == "stale"
    assert attempts[-1].error_code == "LEASE_EXPIRED"
    s2.close()


# --- capacity / waiting_capacity ------------------------------------------


def test_waiting_capacity_is_not_business_failure(db):
    job = add_job(db)
    claimed = claim(db, job.id, "w1", now=BASE)
    parked = queue.move_to_waiting_capacity(db, job.id, "w1", claimed.claim_token, now=at(1))
    row = reload(db, job.id)
    assert row.status == "waiting_capacity"
    assert row.outcome is None
    assert row.last_error_code is None
    assert row.finished_at is None
    assert parked.execution_count == 1
    assert parked.attempts == 0


def test_capacity_transition_does_not_increment_attempts(db):
    job = add_job(db)
    claimed = claim(db, job.id, "w1", now=BASE)
    assert claimed.execution_count == 1
    queue.move_to_waiting_capacity(db, job.id, "w1", claimed.claim_token, now=at(1))
    assert reload(db, job.id).execution_count == 1
    assert reload(db, job.id).attempts == 0


def test_promote_waiting_capacity_becomes_claimable(session_factory):
    s = session_factory()
    job = add_job(s, kind="p")
    s.close()

    s1 = session_factory()
    claimed = claim(s1, job.id, "w1", now=BASE)
    queue.move_to_waiting_capacity(s1, job.id, "w1", claimed.claim_token, now=at(1))
    assert queue.claim_next_job(s1, "w2", 60, now=at(2)) is None  # not queued yet
    queued_again = queue.promote_waiting_capacity(s1, job.id, now=at(3))
    assert queued_again.status == "queued"
    assert reload(s1, job.id).execution_count == 1  # parking never cost an execution
    s1.close()

    s2 = session_factory()
    reclaim = queue.claim_next_job(s2, "w2", 60, now=at(4))
    assert reclaim is not None and reclaim.execution_count == 2
    s2.close()


def test_runner_capacity_gate_blocks_over_concurrency(db):
    runner = add_runner(db, max_concurrency=2)
    jobs = [add_job(db, kind=f"c{i}") for i in range(2)]

    j1 = queue.claim_next_job(db, "w1", 60, runner=runner, now=BASE)
    j2 = queue.claim_next_job(db, "w1", 60, runner=runner, now=at(1))
    assert j1 is not None and j2 is not None
    assert reload_runner(db, runner.id).active_count == 2
    assert reload_runner(db, runner.id).state == "busy"

    third = add_job(db, kind="c3")
    with pytest.raises(queue.RunnerAtCapacity):
        queue.claim_next_job(db, "w1", 60, runner=runner, now=at(2))

    queue.complete_job(db, j1.id, "w1", j1.claim_token, outcome="success", now=at(3))
    assert reload_runner(db, runner.id).active_count == 1
    assert reload_runner(db, runner.id).state == "ready"

    after = queue.claim_next_job(db, "w1", 60, runner=runner, now=at(4))
    assert after is not None and after.id == third.id


def test_runner_unavailable_states_block_claim(db):
    for state in (RunnerState.OFFLINE, RunnerState.DISABLED, RunnerState.AUTH_ERROR):
        runner = add_runner(db, state=state.value)
        job = add_job(db, kind=state.value)
        with pytest.raises(queue.RunnerUnavailable):
            queue.claim_next_job(db, "w1", 60, runner=runner, now=BASE)


def test_attempt_created_at_dispatch_and_closed_on_finalize(db):
    runner = add_runner(db)
    job = add_job(db)
    claimed = claim(db, job.id, "w1", now=BASE, runner=runner)

    open_attempt = queue.get_open_attempt(db, job.id)
    assert open_attempt is not None, "attempt must exist at dispatch start"
    assert open_attempt.result_type is None
    assert open_attempt.runner_instance_id == runner.id
    assert open_attempt.attempt_number == 1
    assert open_attempt.started_at == BASE

    queue.complete_job(db, job.id, "w1", claimed.claim_token, outcome="success",
                       checkpoint_before={"pos": 1}, checkpoint_after={"pos": 2}, now=at(5))
    assert queue.get_open_attempt(db, job.id) is None
    rec = db.scalars(
        select(RunnerAttempt).where(RunnerAttempt.pipeline_job_id == job.id)
        .execution_options(populate_existing=True)
    ).all()
    assert len(rec) == 1
    assert rec[0].result_type == "success"
    assert rec[0].finished_at == at(5)
    assert rec[0].checkpoint_before == {"pos": 1}
    assert rec[0].checkpoint_after == {"pos": 2}


# --- attempts/infra counter helpers ---------------------------------------


def test_business_failure_requeues_then_fails(db):
    job = add_job(db, max_attempts=2)
    claimed = claim(db, job.id, "w1", now=BASE)
    r1 = queue.business_failure(db, job.id, "w1", claimed.claim_token, error_code="bad", now=at(1))
    assert r1.status == "queued"
    assert r1.attempts == 1
    assert reload(db, job.id).infrastructure_failures == 0

    c2 = claim(db, job.id, "w1", now=at(2))
    r2 = queue.business_failure(db, job.id, "w1", c2.claim_token, error_code="bad", now=at(3))
    assert r2.status == "failed"
    assert r2.attempts == 2


def test_infra_failure_does_not_touch_business_attempts(db):
    job = add_job(db, max_attempts=5, max_infra_attempts=5)
    claimed = claim(db, job.id, "w1", now=BASE)
    r = queue.infra_failure(db, job.id, "w1", claimed.claim_token, result_type="runner_crashed",
                            error_message="boom", now=at(1))
    assert r.status == "queued"
    assert r.attempts == 0
    assert r.infrastructure_failures == 1


def test_quota_exhausted_releases_slot_and_parks_runner(db):
    runner = add_runner(db)
    job = add_job(db)
    claimed = claim(db, job.id, "w1", now=BASE, runner=runner)
    quota_at = at(10)
    r = queue.quota_exhausted(db, job.id, "w1", claimed.claim_token, quota_reset_at=quota_at, now=at(1))
    assert r.status == "queued"
    assert r.attempts == 0
    assert reload(db, job.id).infrastructure_failures == 0
    rr = reload_runner(db, runner.id)
    assert rr.state == "quota_exhausted"
    assert rr.quota_reset_at == quota_at
    assert rr.active_count == 0, "quota failure must release the capacity slot"


def test_rate_limited_cooldowns_runner_and_requeues(db):
    runner = add_runner(db)
    job = add_job(db)
    claimed = claim(db, job.id, "w1", now=BASE, runner=runner)
    r = queue.rate_limited(db, job.id, "w1", claimed.claim_token, retry_after=15, now=at(1))
    assert reload(db, job.id).status == "queued"
    assert reload(db, job.id).scheduled_at == at(16)
    rr = reload_runner(db, runner.id)
    assert rr.state == "cooldown"
    assert rr.cooldown_until == at(16)
    assert rr.active_count == 0


# --- request-session model -------------------------------------------------


def test_sessions_and_runners_are_linkable(db):
    session = WorkflowSession(mode="auto", status="active", all_agents_unavailable_policy="pause_auto_resume")
    db.add(session)
    db.commit()
    runner = RunnerInstance(workflow_session_id=session.id, runner_type="story", enabled=True)
    db.add(runner)
    db.commit()
    assert db.get(RunnerInstance, runner.id).workflow_session_id == session.id