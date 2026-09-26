"""Phase 5 Workstream A: WorkflowService / RunnerService (the only mutation boundary).

Deterministic: injected clock, real SQLite, no sleeps. Concurrency tests use barrier-started
threads that each build their own service/orchestrator.
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from sqlalchemy import func, select, update

from storyflow import queue
from storyflow.agents import FakeRunner, RunnerRegistry
from storyflow.dispatcher import Dispatcher, DispatchOutcome
from storyflow.errors import Conflict, InvalidState, NotFound, NotRetryable, StoryFlowError, ValidationFailed
from storyflow.policy import RECOMMENDED, RECOMMENDED_BATCH_SETTINGS
from storyflow.presets import PRESETS
from storyflow.models import (
    AudioGeneration, CanonAnalysis, ChannelWorkflow, ChannelWorkflowStatus, JobStatus, PipelineJob,
    RunnerInstance, StoryGeneration, StoryProject, StoryVersion, TTSGeneration, WorkflowSession,
)
from storyflow.protocol import ResultCode, RunnerResult
from storyflow.roles import Role
from storyflow.services import RunnerService, WorkflowService, slugify
from test_phase4_e2e import NOW, ROLES, Env
from test_phase4_e2e import env as _env  # noqa: F401  (reuse the phase-4 fixture)
from storyflow.story_steps import FakeStoryPipelineRunner

S = ChannelWorkflowStatus


@pytest.fixture
def env(_env):  # noqa: F811
    return _env


def svc(env):
    return WorkflowService(env.ctx, env.orchestrator())


def new_draft(env, name="w", projects=1, **kw):
    s = svc(env)
    r = s.create_workflow(name, **kw)
    for i in range(projects):
        s.add_project(r.workflow_id, f"Title {i}")
    return s, r.workflow_id


def force(env, wid, status, reason=None, detail=None):
    db = env.sf()
    db.execute(update(ChannelWorkflow).where(ChannelWorkflow.id == wid)
               .values(status=status, status_reason=reason, status_detail=detail))
    db.commit()
    db.close()


def wf_row(env, wid):
    return env.q(lambda db: db.get(ChannelWorkflow, wid, populate_existing=True))


def run_threads(n, fn):
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        try:
            return fn(i)
        except StoryFlowError as exc:
            return exc
    with ThreadPoolExecutor(n) as pool:
        return list(pool.map(worker, range(n)))


# --- create ---------------------------------------------------------------------


def test_create_makes_draft_with_one_session(env):
    before = env.count(WorkflowSession)
    r = svc(env).create_workflow("  My channel ", config={"a": 1}, role_preferences={"story_writer": ["x"]})
    assert r.changed and r.status == "draft"
    wf = wf_row(env, r.workflow_id)
    assert wf.name == "My channel" and wf.workflow_session_id
    assert wf.config == {"a": 1, "failure_policy": RECOMMENDED, "batch": RECOMMENDED_BATCH_SETTINGS,
                         "preset": "fast", "review": PRESETS["fast"]["review"]}  # applied defaults are recorded
    assert env.count(WorkflowSession) == before + 1
    sess = env.q(lambda db: db.get(WorkflowSession, wf.workflow_session_id))
    assert sess.role_preferences == {"story_writer": ["x"]}


@pytest.mark.parametrize("kwargs,reason", [
    ({"name": ""}, "name_required"), ({"name": "x" * 129}, "name_too_long"),
    ({"name": "a", "config": []}, "config_not_object"), ({"name": "a", "config": {"k": object()}}, "config_not_json"),
    ({"name": "a", "all_agents_unavailable_policy": "nope"}, "invalid_policy"),
    ({"name": "a", "role_preferences": {"bogus": []}}, "invalid_role_preferences"),
    ({"name": "a", "client_key": ""}, "invalid_client_key"), ({"name": "a", "mode": ""}, "invalid_mode"),
])
def test_create_validation(env, kwargs, reason):
    sessions = env.count(WorkflowSession)
    with pytest.raises(ValidationFailed) as ei:
        svc(env).create_workflow(**kwargs)
    assert ei.value.code == "validation" and ei.value.details["reason"] == reason
    assert env.count(WorkflowSession) == sessions


def test_client_key_is_idempotent(env):
    s = svc(env)
    a = s.create_workflow("one", client_key="k1")
    sessions = env.count(WorkflowSession)
    b = s.create_workflow("one", client_key="k1")
    assert a.changed and not b.changed and a.workflow_id == b.workflow_id
    assert env.count(WorkflowSession) == sessions


def test_client_key_concurrent_converges_without_orphan_sessions(env):
    sessions = env.count(WorkflowSession)
    out = run_threads(6, lambda i: svc(env).create_workflow("race", client_key="same"))
    assert not any(isinstance(o, Exception) for o in out)
    assert len({o.workflow_id for o in out}) == 1
    assert sum(o.changed for o in out) == 1
    assert env.count(ChannelWorkflow, ChannelWorkflow.client_key == "same") == 1
    assert env.count(WorkflowSession) == sessions + 1  # lost races rolled back their session row


# --- add_project ------------------------------------------------------------------


def test_add_project_slug_rules(env):
    s = svc(env)
    wid = s.create_workflow("w").workflow_id
    other = s.create_workflow("o").workflow_id
    a = s.add_project(wid, "Hello World!")
    b = s.add_project(wid, "Hello World!")
    c = s.add_project(wid, "Hello World!")
    assert [a.detail["slug"], b.detail["slug"], c.detail["slug"]] == ["hello-world", "hello-world-2", "hello-world-3"]
    assert a.changed and a.status == "draft"
    given = s.add_project(wid, "X", slug="mine")
    again = s.add_project(wid, "Whatever", slug="mine")
    assert given.changed and not again.changed and again.detail["project_id"] == given.detail["project_id"]
    with pytest.raises(Conflict) as ei:
        s.add_project(other, "X", slug="mine")
    assert ei.value.details["reason"] == "slug_taken"
    assert slugify("!!!") == "project"


def test_add_project_validation(env):
    s = svc(env)
    wid = s.create_workflow("w").workflow_id
    with pytest.raises(ValidationFailed):
        s.add_project(wid, "  ")
    with pytest.raises(ValidationFailed):
        s.add_project(wid, "x" * 256)
    with pytest.raises(NotFound) as ei:
        s.add_project("missing", "t")
    assert ei.value.code == "not_found"


@pytest.mark.parametrize("status,ok", [("draft", True), ("active", True), ("paused", True),
                                       ("finished", False), ("cancelled", False), ("abandoned", False)])
def test_add_project_by_status(env, status, ok):
    s, wid = new_draft(env, projects=0)
    force(env, wid, status)
    if ok:
        assert s.add_project(wid, "t").changed
    else:
        with pytest.raises(InvalidState) as ei:
            s.add_project(wid, "t")
        assert ei.value.details["reason"] == "workflow_closed"
        assert env.count(StoryProject, StoryProject.channel_workflow_id == wid) == 0


def test_add_project_concurrent_slugs_are_unique(env):
    s, wid = new_draft(env, projects=0)
    out = run_threads(6, lambda i: svc(env).add_project(wid, "Same Title"))
    assert not any(isinstance(o, Exception) for o in out)
    assert len({o.detail["slug"] for o in out}) == 6 and all(o.changed for o in out)


def test_add_project_same_given_slug_concurrent_is_one_row(env):
    s, wid = new_draft(env, projects=0)
    out = run_threads(5, lambda i: svc(env).add_project(wid, "T", slug="fixed"))
    assert len({o.detail["project_id"] for o in out}) == 1 and sum(o.changed for o in out) == 1
    assert env.count(StoryProject, StoryProject.slug == "fixed") == 1


# --- lifecycle: table-driven ------------------------------------------------------------

OP, ERR = "ok", "err"
TABLE = [
    # (status, reason, command, expectation)
    ("draft", None, "start", (OP, "active", True)),
    ("active", None, "start", (OP, "active", False)),
    ("paused", "operator", "start", (ERR, InvalidState, "use_resume")),
    ("finished", None, "start", (ERR, InvalidState, "terminal")),
    ("cancelled", None, "start", (ERR, InvalidState, "terminal")),
    ("active", None, "pause", (OP, "paused", True)),
    ("paused", "operator", "pause", (OP, "paused", False)),
    ("paused", "step_failed", "pause", (OP, "paused", False)),
    ("draft", None, "pause", (ERR, InvalidState, "not_started")),
    ("finished", None, "pause", (ERR, InvalidState, "terminal")),
    ("cancelled", None, "pause", (ERR, InvalidState, "terminal")),
    ("paused", "operator", "resume", (OP, "active", True)),
    ("paused", None, "resume", (OP, "active", True)),
    ("active", None, "resume", (OP, "active", False)),
    ("paused", "step_failed", "resume", (ERR, InvalidState, "has_failed_steps")),
    ("draft", None, "resume", (ERR, InvalidState, "not_started")),
    ("finished", None, "resume", (ERR, InvalidState, "terminal")),
    ("draft", None, "cancel", (OP, "cancelled", True)),
    ("active", None, "cancel", (OP, "cancelled", True)),
    ("paused", "step_failed", "cancel", (OP, "cancelled", True)),
    ("cancelled", None, "cancel", (OP, "cancelled", False)),
    ("finished", None, "cancel", (ERR, InvalidState, "already_finished")),
    ("abandoned", None, "cancel", (ERR, InvalidState, "terminal")),
    ("finished", None, "retry", (ERR, InvalidState, "terminal")),
    ("cancelled", None, "retry", (ERR, InvalidState, "terminal")),
    ("active", None, "retry", (ERR, NotRetryable, "workflow_not_paused")),
    ("draft", None, "retry", (ERR, NotRetryable, "workflow_not_paused")),
    ("paused", "operator", "retry", (ERR, NotRetryable, "not_failed")),
]


@pytest.mark.parametrize("status,reason,command,expect", TABLE)
def test_lifecycle_table(env, status, reason, command, expect):
    s, wid = new_draft(env)
    force(env, wid, status, reason)
    if expect[0] == OP:
        r = getattr(s, command)(wid)
        assert (r.status, r.changed) == (expect[1], expect[2])
        row = wf_row(env, wid)
        assert row.status == expect[1]
        if expect[1] == "cancelled" and expect[2]:
            assert row.finished_at is not None and row.status_reason is None
        if command == "resume" and expect[2]:
            assert row.status_reason is None
        if command == "pause" and expect[2]:
            assert row.status_reason == "operator"
    else:
        with pytest.raises(expect[1]) as ei:
            getattr(s, command)(wid)
        assert ei.value.details["reason"] == expect[2] and ei.value.code
        assert wf_row(env, wid).status == status


def test_start_empty_workflow_rejected(env):
    s, wid = new_draft(env, projects=0)
    with pytest.raises(InvalidState) as ei:
        s.start(wid)
    assert ei.value.details["reason"] == "no_projects" and wf_row(env, wid).status == "draft"


def test_start_does_not_dispatch_or_enqueue(env):
    s, wid = new_draft(env)
    s.start(wid)
    assert env.jobs() == []


def test_missing_workflow_is_not_found(env):
    for cmd in ("start", "pause", "resume", "retry", "cancel"):
        with pytest.raises(NotFound):
            getattr(svc(env), cmd)("nope")


def test_pause_does_not_clobber_step_failed_detail(env):
    s, wid = new_draft(env)
    force(env, wid, "paused", "step_failed", {"project_id": "p", "step": "story", "error_code": "e"})
    r = s.pause(wid)
    assert not r.changed
    row = wf_row(env, wid)
    assert row.status_reason == "step_failed" and row.status_detail["step"] == "story"


def test_pause_stops_new_scheduling_and_resume_continues(env):
    env.add_router()
    s = svc(env)
    s.pause(env.workflow_id)
    orch = env.orchestrator()
    rnd = orch.run_round(env.workflow_id)
    assert rnd.outcome is None and env.jobs() == []
    assert s.resume(env.workflow_id).changed
    assert orch.run_until_idle(env.workflow_id).status == "completed"


# --- retry after a failed step ----------------------------------------------------------


def failed_env(env):
    story = FakeStoryPipelineRunner(env.store, emit_invalid={"story"})
    env.add_router(story=story)
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "paused"
    return story


def test_retry_after_step_failure_creates_fresh_generation(env):
    story = failed_env(env)
    s = svc(env)
    row = wf_row(env, env.workflow_id)
    assert row.status_reason == "step_failed" and row.status_detail["step"] == "story"
    with pytest.raises(InvalidState) as ei:
        s.resume(env.workflow_id)
    assert ei.value.details["reason"] == "has_failed_steps"
    assert "claim" not in str(ei.value.details)

    completed_before = env.q(lambda db: [(c.id, c.status) for c in db.scalars(select(CanonAnalysis))])
    story.emit_invalid = False
    r = s.retry(env.workflow_id)
    assert r.changed and r.status == "active"
    assert wf_row(env, env.workflow_id).status_detail is None
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "completed"
    gens = env.q(lambda db: db.scalars(select(StoryGeneration)).all())
    assert sorted(g.status for g in gens) == ["completed", "failed"]
    assert env.count(StoryVersion) == 1
    assert env.q(lambda db: [(c.id, c.status) for c in db.scalars(select(CanonAnalysis))]) == completed_before
    # finished workflow is terminal: retry is a stable InvalidState
    with pytest.raises(InvalidState):
        s.retry(env.workflow_id)


def test_retry_with_project_id(env):
    story = failed_env(env)
    s = svc(env)
    story.emit_invalid = False
    other_wf = new_draft(env)[1]
    other_pid = env.q(lambda db: db.scalar(select(StoryProject.id).where(StoryProject.channel_workflow_id == other_wf)))
    with pytest.raises(NotFound) as ei:
        s.retry(env.workflow_id, project_id=other_pid)
    assert ei.value.details["reason"] == "project_not_in_workflow"
    r = s.retry(env.workflow_id, project_id=env.project_id)
    assert r.changed and r.detail["step"] == "story" and r.status == "active"


def test_retry_concurrent_has_single_winner_and_no_duplicate_rows(env):
    story = failed_env(env)
    story.emit_invalid = False
    out = run_threads(5, lambda i: svc(env).retry(env.workflow_id))
    wins = [o for o in out if not isinstance(o, Exception) and o.changed]
    assert len(wins) == 1
    assert all(isinstance(o, NotRetryable) or not o.changed for o in out if o is not wins[0])
    gens = env.q(lambda db: db.scalars(select(StoryGeneration)).all())
    assert sorted(g.status for g in gens) in (["failed", "processing"], ["failed", "queued"])
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "completed"
    assert env.count(StoryVersion) == 1


# --- cancel ---------------------------------------------------------------------------------


def test_cancel_sweeps_pending_jobs_and_open_rows(env):
    orch = env.orchestrator()
    orch.tick(env.workflow_id)  # source done, canon job queued (no runner)
    orch.run_round(env.workflow_id)  # no runner in session -> parked
    job = env.jobs()[0]
    assert job.status == JobStatus.WAITING_CAPACITY.value
    r = svc(env).cancel(env.workflow_id)
    assert r.changed and r.detail["jobs_cancelled"] == 1 and r.detail["domain_rows_cancelled"] == 1
    assert env.jobs()[0].status == JobStatus.CANCELLED.value
    canon = env.q(lambda db: db.scalars(select(CanonAnalysis)).one())
    assert canon.status == "cancelled"
    # inert afterwards: no new work, workflow never resurrected
    env.add_router()
    for _ in range(3):
        env.orchestrator().run_round(env.workflow_id)
    assert wf_row(env, env.workflow_id).status == "cancelled" and len(env.jobs()) == 1


def test_cancel_keeps_completed_history_and_is_idempotent(env):
    env.add_router()
    orch = env.orchestrator()
    while not env.q(lambda db: db.scalars(select(StoryVersion)).all()):
        orch.run_round(env.workflow_id)
    # tts is queued now, canon/story completed
    s = svc(env)
    r = s.cancel(env.workflow_id)
    assert r.changed and r.detail["jobs_cancelled"] == 1
    assert env.q(lambda db: [c.status for c in db.scalars(select(CanonAnalysis))]) == ["completed"]
    assert env.q(lambda db: [g.status for g in db.scalars(select(StoryGeneration))]) == ["completed"]
    assert env.q(lambda db: [v.status for v in db.scalars(select(StoryVersion))]) == ["active"]
    assert env.q(lambda db: [t.status for t in db.scalars(select(TTSGeneration))]) == ["cancelled"]
    again = s.cancel(env.workflow_id)
    assert not again.changed and again.detail["jobs_cancelled"] == 0
    assert wf_row(env, env.workflow_id).finished_at is not None


def test_cancel_resweeps_work_a_racing_tick_enqueued(env):
    env.add_router()
    s = svc(env)
    s.cancel(env.workflow_id)
    # simulate the racing tick: a job + domain row created after the status flip
    orch = env.orchestrator()
    db = env.sf()
    proj = db.get(StoryProject, env.project_id)
    env.ctx.subtitle_client  # source runs inline
    from storyflow.story_steps import CanonStep, SourceStep
    SourceStep().run(env.ctx, env.project_id)
    canon_id, spec = CanonStep().begin(db, env.ctx, proj)
    job = queue.enqueue_job(db, kind=spec.kind, payload=spec.payload, dedupe_key=spec.dedupe_key,
                            session_id=env.session_id, role=spec.role, now=NOW)
    CanonStep().link_job(db, env.ctx, canon_id, job)
    db.close()
    r = s.cancel(env.workflow_id)
    assert not r.changed and r.detail["jobs_cancelled"] == 1
    assert env.jobs()[0].status == JobStatus.CANCELLED.value
    assert env.q(lambda db: db.scalars(select(CanonAnalysis)).one().status) == "cancelled"
    assert orch.run_round(env.workflow_id).outcome is None


def test_cancel_leaves_processing_job_and_late_completion_changes_nothing(env):
    iid, router = env.add_router()
    orch = env.orchestrator()
    orch.tick(env.workflow_id)
    db = env.sf()
    claimed = queue.claim_next_job(db, iid, 60, runner=db.get(RunnerInstance, iid), now=NOW)
    assert claimed.status == "processing"
    db.close()

    r = svc(env).cancel(env.workflow_id)
    assert r.changed and r.detail["jobs_cancelled"] == 0
    assert env.jobs()[0].status == JobStatus.PROCESSING.value  # left alone

    db = env.sf()
    runner = db.get(RunnerInstance, iid)
    out = env.orchestrator().dispatcher.apply_result(db, claimed, runner, RunnerResult(code=ResultCode.SUCCESS), NOW)
    db.close()
    assert out is DispatchOutcome.DISPATCHED_SUCCESS
    assert env.jobs()[0].status == JobStatus.COMPLETED.value
    assert env.q(lambda db: db.get(RunnerInstance, iid, populate_existing=True).active_count) == 0

    for _ in range(3):
        rnd = env.orchestrator().run_round(env.workflow_id)
        assert rnd.outcome is None
    assert wf_row(env, env.workflow_id).status == "cancelled"
    assert len(env.jobs()) == 1  # no downstream step scheduled
    assert env.count(StoryGeneration) == 0 and env.count(TTSGeneration) == 0 and env.count(AudioGeneration) == 0


def test_cancel_concurrent_single_winner(env):
    env.add_router()
    env.orchestrator().tick(env.workflow_id)
    out = run_threads(6, lambda i: svc(env).cancel(env.workflow_id))
    assert not any(isinstance(o, Exception) for o in out)
    assert sum(o.changed for o in out) == 1
    assert wf_row(env, env.workflow_id).status == "cancelled"
    assert [j.status for j in env.jobs()] == ["cancelled"]


def test_concurrent_start_and_mixed_commands_are_deterministic(env):
    s, wid = new_draft(env)
    out = run_threads(6, lambda i: svc(env).start(wid))
    assert sum(o.changed for o in out) == 1 and all(o.status == "active" for o in out)
    out = run_threads(6, lambda i: svc(env).pause(wid))
    assert sum(o.changed for o in out) == 1
    out = run_threads(6, lambda i: svc(env).resume(wid))
    assert sum(o.changed for o in out) == 1 and wf_row(env, wid).status == "active"


def test_pause_vs_cancel_race_ends_cancelled(env):
    s, wid = new_draft(env)
    s.start(wid)

    def fn(i):
        return svc(env).pause(wid) if i % 2 == 0 else svc(env).cancel(wid)
    out = run_threads(6, fn)
    assert all(isinstance(o, InvalidState) or not isinstance(o, Exception) for o in out)
    assert wf_row(env, wid).status == "cancelled"


def test_errors_carry_codes_and_no_secrets(env):
    s, wid = new_draft(env)
    for call in (lambda: s.resume(wid), lambda: s.retry(wid), lambda: s.add_project("nope", "t")):
        with pytest.raises(StoryFlowError) as ei:
            call()
        text = repr(ei.value.details) + ei.value.message
        assert ei.value.code in ("invalid_state", "not_retryable", "not_found")
        assert "claim_token" not in text and "\\" not in text and "/" not in text


# --- runner service ---------------------------------------------------------------------------


def make_session(env):
    db = env.sf()
    sess = WorkflowSession(mode="auto", status="active", role_preferences={})
    db.add(sess)
    db.commit()
    sid = sess.id
    db.close()
    return sid


def make_runner(env, session_id=None, **kw):
    db = env.sf()
    r = RunnerInstance(workflow_session_id=session_id, runner_type=kw.pop("runner_type", "fake"),
                       supported_roles=kw.pop("supported_roles", ["general_worker"]), **kw)
    db.add(r)
    db.commit()
    rid = r.id
    db.close()
    return rid


def runner_row(env, rid):
    return env.q(lambda db: db.get(RunnerInstance, rid, populate_existing=True))


def rsvc(env):
    return RunnerService(env.sf, env.ctx.clock)


def test_assign_and_unassign_runner(env):
    _, wid = new_draft(env)
    rid = make_runner(env, state="quota_exhausted", quota_reset_at=datetime(2030, 1, 1))
    sid = wf_row(env, wid).workflow_session_id
    r = rsvc(env).assign_runner(rid, workflow_id=wid, roles=["story_writer", "tts_adapter"])
    assert r.changed and r.workflow_session_id == sid
    row = runner_row(env, rid)
    assert row.supported_roles == ["story_writer", "tts_adapter"]
    assert row.state == "quota_exhausted" and row.quota_reset_at == datetime(2030, 1, 1)  # untouched
    again = rsvc(env).assign_runner(rid, workflow_id=wid)
    assert not again.changed
    other = make_session(env)
    with pytest.raises(Conflict) as ei:
        rsvc(env).assign_runner(rid, session_id=other)
    assert ei.value.details["reason"] == "assigned_to_other_session"
    assert runner_row(env, rid).workflow_session_id == sid
    assert rsvc(env).unassign_runner(rid).changed
    assert not rsvc(env).unassign_runner(rid).changed
    assert runner_row(env, rid).workflow_session_id is None
    assert rsvc(env).assign_runner(rid, session_id=other).changed


def test_assign_validation(env):
    _, wid = new_draft(env)
    rid = make_runner(env)
    with pytest.raises(ValidationFailed):
        rsvc(env).assign_runner(rid, workflow_id=wid, roles=["bogus"])
    with pytest.raises(ValidationFailed):
        rsvc(env).assign_runner(rid)
    with pytest.raises(NotFound):
        rsvc(env).assign_runner("nope", workflow_id=wid)
    with pytest.raises(NotFound):
        rsvc(env).assign_runner(rid, workflow_id="nope")
    force(env, wid, "cancelled")
    with pytest.raises(InvalidState):
        rsvc(env).assign_runner(rid, workflow_id=wid)
    assert runner_row(env, rid).workflow_session_id is None


def test_assign_concurrent_one_winner(env):
    rid = make_runner(env)
    sessions = [make_session(env) for _ in range(4)]
    out = run_threads(4, lambda i: rsvc(env).assign_runner(rid, session_id=sessions[i]))
    wins = [o for o in out if not isinstance(o, Exception)]
    assert len(wins) == 1 and all(isinstance(o, Conflict) for o in out if o not in wins)
    assert runner_row(env, rid).workflow_session_id == wins[0].workflow_session_id


def test_unassign_busy_runner_conflicts(env):
    sid = make_session(env)
    rid = make_runner(env, sid, active_count=1, state="busy")
    with pytest.raises(Conflict) as ei:
        rsvc(env).unassign_runner(rid)
    assert ei.value.details["reason"] == "runner_busy"
    assert runner_row(env, rid).workflow_session_id == sid


def test_set_runner_enabled_rules(env):
    rid = make_runner(env)
    s = rsvc(env)
    assert not s.set_runner_enabled(rid, True).changed
    r = s.set_runner_enabled(rid, False)
    assert r.changed and not r.enabled and r.state == "disabled"
    assert not s.set_runner_enabled(rid, False).changed
    r = s.set_runner_enabled(rid, True)
    assert r.changed and r.enabled and r.state == "ready"

    # future cooldown / quota windows are preserved across a disable/enable cycle
    cd = make_runner(env, state="cooldown", cooldown_until=datetime(2030, 1, 1))
    s.set_runner_enabled(cd, False)
    assert s.set_runner_enabled(cd, True).state == "cooldown"
    qx = make_runner(env, state="quota_exhausted", quota_reset_at=datetime(2030, 1, 1))
    s.set_runner_enabled(qx, False)
    assert s.set_runner_enabled(qx, True).state == "quota_exhausted"
    # past window -> ready
    old = make_runner(env, state="cooldown", cooldown_until=datetime(2020, 1, 1))
    s.set_runner_enabled(old, False)
    assert s.set_runner_enabled(old, True).state == "ready"

    # AUTH_ERROR is never resurrected
    ae = make_runner(env, state="auth_error")
    assert s.set_runner_enabled(ae, False).state == "auth_error"
    assert s.set_runner_enabled(ae, True).state == "auth_error"
    # busy runner: disabled flag only, state and slot untouched
    busy = make_runner(env, state="busy", active_count=1)
    r = s.set_runner_enabled(busy, False)
    assert r.state == "busy" and not r.enabled and runner_row(env, busy).active_count == 1
    with pytest.raises(NotFound):
        s.set_runner_enabled("nope", True)


def test_runner_joins_a_session_only_through_assignment(env):
    """Dispatcher-level isolation: unassigned and foreign-session runners get nothing."""
    s, wid = new_draft(env)
    sess_a = wf_row(env, wid).workflow_session_id
    sess_b = make_session(env)
    fake = FakeRunner("fake")
    rid = make_runner(env)  # unassigned (as discovery creates it)
    reg = RunnerRegistry()
    reg.register(rid, fake)
    disp = Dispatcher(reg)
    db = env.sf()
    job = queue.enqueue_job(db, kind="k", dedupe_key="iso:1", session_id=sess_a, now=NOW)
    session_a = db.get(WorkflowSession, sess_a)

    outcome, _ = disp.run_round(db, session_a, now=NOW)
    assert outcome is DispatchOutcome.PARKED_NO_CANDIDATE and fake.invocations == []

    rsvc(env).assign_runner(rid, session_id=sess_b)  # foreign session
    outcome, _ = disp.run_round(db, session_a, now=NOW)
    assert fake.invocations == [] and outcome is not DispatchOutcome.DISPATCHED_SUCCESS

    rsvc(env).unassign_runner(rid)
    rsvc(env).assign_runner(rid, workflow_id=wid)
    outcome, _ = disp.run_round(db, session_a, now=NOW)
    assert outcome is DispatchOutcome.DISPATCHED_SUCCESS and len(fake.invocations) == 1
    assert db.get(PipelineJob, job.id, populate_existing=True).status == "completed"
    db.close()
