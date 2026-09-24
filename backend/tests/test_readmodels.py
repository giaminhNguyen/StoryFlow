"""Phase 5 read models: derived, read-only, JSON-safe, no secrets, no absolute paths."""

import json
import re
from datetime import timedelta

import pytest
from sqlalchemy import event, select, text

from storyflow.agents import CRASH, FakeRunner
from storyflow.database import Base
from storyflow.errors import NotFound, ValidationFailed
from storyflow.models import (
    AudioChunk, AudioGeneration, ChannelWorkflow, ChannelWorkflowStatus, PipelineJob, RunnerInstance,
    StoryProject,
)
from storyflow.protocol import ResultCode
from storyflow.readmodels import ReadModels, categorize_error, effective_runner_state, to_jsonable
from storyflow.roles import Role
from storyflow.story_steps import FakeStoryPipelineRunner
from test_phase4_e2e import NOW, ROLES, Env, FakeAudioRunner

FORBIDDEN_KEYS = {"claim_token", "worker_id", "lease_expires_at", "lease", "database_url", "db_url"}
_ABS = re.compile(r"[A-Za-z]:[\\/]")


@pytest.fixture
def env(session_factory, tmp_path):
    return Env(session_factory, tmp_path)


def rm(env) -> ReadModels:
    return ReadModels(env.ctx, env.orchestrator().chain)


def set_wf(env, **values):
    db = env.sf()
    wf = db.get(ChannelWorkflow, env.workflow_id)
    for k, v in values.items():
        setattr(wf, k, v)
    db.commit()
    db.close()


def walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)
    else:
        yield obj


def assert_clean(env, tmp_path, payload):
    blob = json.dumps(payload)
    json.loads(blob)  # JSON-safe
    for item in walk(payload):
        if isinstance(item, str):
            assert item not in FORBIDDEN_KEYS, item
            assert not _ABS.search(item), item
            assert not item.startswith("/"), item
            assert ".." not in item.replace("...", ""), item
            assert str(tmp_path) not in item and tmp_path.as_posix() not in item
    assert str(tmp_path) not in blob and tmp_path.as_posix() not in blob


def finish(env):
    env.add_router()
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "completed"


def to_chunks_missing(env, *, workflow_status="active"):
    """Phase 4 shape: audio job COMPLETED but AudioGeneration not fully registered."""
    db = env.sf()
    audio = db.scalar(select(AudioGeneration))
    last = db.scalars(select(AudioChunk).order_by(AudioChunk.chunk_index.desc())).first()
    db.delete(last)
    audio.status, audio.error_code, audio.error_message = "processing", "chunks_missing", "1 of N chunks missing"
    db.commit()
    db.close()
    set_wf(env, status=workflow_status)


# --- lifecycle / display_state -------------------------------------------------------------


def test_empty_draft(env, session_factory):
    db = session_factory()
    wf = ChannelWorkflow(name="d", mode="auto", status=ChannelWorkflowStatus.DRAFT.value)
    db.add(wf)
    db.commit()
    wid = wf.id
    db.close()
    snap = rm(env).get_workflow(wid)
    assert snap.display_state == "draft" and snap.project_count == 0 and snap.projects == []
    assert snap.capacity.registered == 0 and snap.capacity.message == "workflow has no runner session"


def test_current_step_progression_from_db(env):
    env.add_router()
    orch = env.orchestrator()
    seen = []

    def observe():
        p = rm(env).get_project(env.project_id)
        seen.append((p.current_step, p.step_status, p.state))
        return p

    p = observe()
    assert seen[0] == ("source", "not_started", "not_started")
    assert [s.step for s in p.steps] == ["source", "canon", "story", "tts", "audio"]
    orch.tick(env.workflow_id)
    p = observe()
    assert p.current_step == "canon" and p.source is not None and p.source.snapshot_number == 1
    assert p.canon.status == "queued"
    while env.wf_status() != ChannelWorkflowStatus.FINISHED.value:
        orch.run_round(env.workflow_id)
        observe()
    steps = [s for s, _, _ in seen]
    for expected in ("source", "canon", "story", "tts", "audio", None):
        assert expected in steps
    p = rm(env).get_project(env.project_id)
    assert p.current_step is None and p.state == "completed" and p.block is None and p.failure is None
    assert all(s.status == "completed" for s in p.steps)
    assert [s.job.kind for s in p.steps[1:]] == ["canon_analysis", "story_generation", "tts_generation",
                                                 "audio_generation"]
    assert p.steps[0].job is None


def test_finished_snapshot_is_relative_and_complete(env, tmp_path):
    finish(env)
    r = rm(env)
    snap = r.get_workflow(env.workflow_id)
    assert snap.display_state == "completed" and snap.counts.completed == 1 and snap.project_count == 1
    p = snap.projects[0]
    assert p.source.artifact_path.startswith("projects/") and p.source.content_hash
    assert p.source.language_code == "en" and p.canon.has_canon
    assert p.story_generation.status == "completed" and p.story_version.word_count > 0
    assert p.story_version.content_path.startswith("projects/")
    assert p.tts.voice == "narrator" and p.tts.chunk_count and p.tts.chunk_count == p.audio.chunk_count
    assert p.audio.run_number == 1 and p.audio.store_dir.startswith("projects/")
    assert [c.chunk_index for c in p.audio.chunks] == list(range(1, p.audio.chunk_count + 1))
    assert all(c.artifact_path.startswith("projects/") and c.duration_ms > 0 for c in p.audio.chunks)
    assert_clean(env, tmp_path, to_jsonable(snap))
    assert_clean(env, tmp_path, to_jsonable(r.list_workflows()))
    assert_clean(env, tmp_path, to_jsonable(r.list_runners()))


def test_unsafe_stored_paths_are_dropped(env, tmp_path):
    finish(env)
    db = env.sf()
    audio = db.scalar(select(AudioGeneration))
    audio.store_dir = str(tmp_path / "abs")
    db.commit()
    db.close()
    p = rm(env).get_project(env.project_id)
    assert p.audio.store_dir is None


def test_paused_by_operator(env):
    env.add_router()
    env.orchestrator().tick(env.workflow_id)
    set_wf(env, status="paused", status_reason="operator")
    snap = rm(env).get_workflow(env.workflow_id)
    assert snap.display_state == "paused" and snap.status_reason == "operator"
    assert snap.projects[0].failure is None


def test_business_failure_pauses_as_failed(env, tmp_path):
    env.add_router(story=FakeStoryPipelineRunner(env.store, emit_invalid={"story"}))
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "paused"
    snap = rm(env).get_workflow(env.workflow_id)
    assert snap.display_state == "failed" and snap.status_reason == "step_failed"
    assert snap.status_detail["step"] == "story" and snap.counts.failed == 1
    p = snap.projects[0]
    assert p.current_step == "story" and p.step_status == "failed" and p.state == "failed"
    f = p.failure
    assert f.category == "business" and f.code == "invalid_output" and f.step == "story"
    assert f.attempts == f.max_attempts and f.infrastructure_failures == 0
    assert len(f.message) <= 400
    assert p.steps[2].job.status == "failed"
    assert_clean(env, tmp_path, to_jsonable(snap))


def test_infrastructure_failure_category(env):
    story = FakeStoryPipelineRunner(env.store, results=[ResultCode.SUCCESS] + [CRASH] * 12)
    env.add_router(story=story)
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "paused"
    snap = rm(env).get_workflow(env.workflow_id)
    f = snap.projects[0].failure
    assert snap.display_state == "failed"
    assert f.category == "infrastructure" and f.code.upper() == "INFRA_EXHAUSTED"
    assert f.infrastructure_failures == f.max_infra_attempts and f.attempts == 0


def test_waiting_capacity_without_runner(env):
    assert env.orchestrator().run_until_idle(env.workflow_id).status == "blocked"
    snap = rm(env).get_workflow(env.workflow_id)
    assert snap.display_state == "waiting_capacity" and snap.counts.waiting_capacity == 1
    p = snap.projects[0]
    assert p.block.kind == "waiting_capacity" and p.state == "waiting_capacity" and p.failure is None
    assert p.steps[1].job.status == "waiting_capacity" and p.steps[1].job.attempts == 0
    cap = snap.capacity
    assert cap.registered == 0 and cap.unserved_roles == [Role.STORY_WRITER.value]
    assert "no runners" in cap.message


def test_waiting_capacity_explains_missing_role(env):
    iid = env._register(env.session_id, FakeRunner("tts_only", roles=[Role.TTS_ADAPTER.value]), "tts_only")
    db = env.sf()
    db.get(RunnerInstance, iid).supported_roles = [Role.TTS_ADAPTER.value]
    db.commit()
    db.close()
    env.orchestrator().run_until_idle(env.workflow_id)
    snap = rm(env).get_workflow(env.workflow_id)
    assert snap.display_state == "waiting_capacity"
    cap = snap.capacity
    assert cap.registered == 1 and cap.ready == 1
    by_role = {r.role: r for r in cap.roles}
    assert by_role["story_writer"].eligible == 0 and by_role["story_writer"].waiting_jobs == 1
    assert by_role["tts_adapter"].eligible == 1
    assert cap.unserved_roles == ["story_writer"] and "story_writer" in cap.message


def test_cancelled_and_abandoned(env):
    env.add_router()
    env.orchestrator().tick(env.workflow_id)
    set_wf(env, status="cancelled")
    assert rm(env).get_workflow(env.workflow_id).display_state == "cancelled"
    set_wf(env, status="abandoned")
    assert rm(env).get_workflow(env.workflow_id).display_state == "cancelled"


def test_chunks_missing_is_blocked_never_complete(env):
    finish(env)
    to_chunks_missing(env)
    snap = rm(env).get_workflow(env.workflow_id)
    p = snap.projects[0]
    assert snap.display_state == "blocked" and snap.counts.blocked == 1
    assert p.block.kind == "chunks_missing" and p.state == "blocked" and p.current_step == "audio"
    assert p.steps[4].job.status == "completed"  # the job itself succeeded
    assert p.audio.registered_chunks < p.audio.chunk_count or p.audio.chunk_count == 0


def test_chunks_missing_on_completed_run_and_finished_workflow(env):
    finish(env)
    db = env.sf()
    db.delete(db.scalars(select(AudioChunk).order_by(AudioChunk.chunk_index.desc())).first())
    db.commit()
    db.close()
    snap = rm(env).get_workflow(env.workflow_id)
    assert snap.status == "finished" and snap.display_state == "blocked"
    assert snap.projects[0].block.kind == "chunks_missing"


def test_finished_but_incomplete_is_inconsistent(env):
    env.add_router()
    env.orchestrator().tick(env.workflow_id)
    set_wf(env, status="finished")
    snap = rm(env).get_workflow(env.workflow_id)
    assert snap.display_state == "blocked"
    assert snap.projects[0].block.kind == "inconsistent" and snap.projects[0].state == "blocked"


def test_delayed_job_is_informational(env):
    env.add_router()
    env.orchestrator().tick(env.workflow_id)
    db = env.sf()
    job = db.scalar(select(PipelineJob))
    job.scheduled_at = NOW + timedelta(hours=1)
    db.commit()
    db.close()
    snap = rm(env).get_workflow(env.workflow_id)
    assert snap.display_state == "active"
    assert snap.projects[0].block.kind == "delayed" and snap.projects[0].block.until == NOW + timedelta(hours=1)


# --- list summary vs full snapshot --------------------------------------------------------


def _s_active(env):
    env.add_router()
    env.orchestrator().tick(env.workflow_id)


def _s_finished(env):
    finish(env)


def _s_failed(env):
    env.add_router(story=FakeStoryPipelineRunner(env.store, emit_invalid={"story"}))
    env.orchestrator().run_until_idle(env.workflow_id)


def _s_infra(env):
    env.add_router(story=FakeStoryPipelineRunner(env.store, results=[ResultCode.SUCCESS] + [CRASH] * 12))
    env.orchestrator().run_until_idle(env.workflow_id)


def _s_waiting(env):
    env.orchestrator().run_until_idle(env.workflow_id)


def _s_cm(env):
    finish(env)
    to_chunks_missing(env)


def _s_cancelled(env):
    _s_active(env)
    set_wf(env, status="cancelled")


def _s_operator(env):
    _s_active(env)
    set_wf(env, status="paused", status_reason="operator")


def _s_draft(env):
    set_wf(env, status="draft")


@pytest.mark.parametrize("build,expected", [
    (_s_active, "active"), (_s_finished, "completed"), (_s_failed, "failed"), (_s_infra, "failed"),
    (_s_waiting, "waiting_capacity"), (_s_cm, "blocked"), (_s_cancelled, "cancelled"),
    (_s_operator, "paused"), (_s_draft, "draft")])
def test_list_summary_matches_full_snapshot(env, build, expected):
    build(env)
    r = rm(env)
    (summary,) = [s for s in r.list_workflows() if s.id == env.workflow_id]
    assert summary.display_state == r.get_workflow(env.workflow_id).display_state == expected
    assert summary.project_count == 1


# --- read-only / restart / secrets ---------------------------------------------------------


def _dump(env):
    db = env.sf()
    try:
        out = {}
        for t in Base.metadata.sorted_tables:
            out[t.name] = sorted(map(repr, db.execute(select(t)).all()))
        return out
    finally:
        db.close()


def test_queries_are_read_only(env, engine):
    _, router = env.add_router()
    env.orchestrator().run_round(env.workflow_id)  # canon done, story queued
    env.orchestrator().run_round(env.workflow_id)
    before = _dump(env)
    invoked = (list(router.steps_seen), list(env.foreign.invocations))
    writes = []

    def spy(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().split(None, 1)[0].upper() in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", spy)
    try:
        r = rm(env)
        r.list_workflows()
        snap = r.get_workflow(env.workflow_id)
        r.get_project(env.project_id)
        r.list_runners()
        r.list_runners(workflow_id=env.workflow_id)
        r.get_runner(snap.runners[0].id)
    finally:
        event.remove(engine, "before_cursor_execute", spy)
    assert writes == []
    assert _dump(env) == before
    assert (list(router.steps_seen), list(env.foreign.invocations)) == invoked


def test_read_does_not_create_missing_rows_or_write_expired_runner_state(env):
    db = env.sf()
    db.add(RunnerInstance(runner_type="q", state="quota_exhausted", quota_reset_at=NOW - timedelta(hours=1)))
    db.commit()
    db.close()
    before = _dump(env)
    (q,) = [x for x in rm(env).list_runners(unassigned=True)]
    assert q.state == "quota_exhausted" and q.effective_state == "ready"
    assert _dump(env) == before


def test_restart_equality(env):
    env.add_router()
    env.orchestrator().run_round(env.workflow_id)
    a, b = rm(env), rm(env)
    assert to_jsonable(a.get_workflow(env.workflow_id)) == to_jsonable(b.get_workflow(env.workflow_id))
    assert to_jsonable(a.list_workflows()) == to_jsonable(b.list_workflows())
    finish_env = env.orchestrator().run_until_idle(env.workflow_id)
    assert finish_env.status == "completed"
    assert to_jsonable(rm(env).get_workflow(env.workflow_id)) == to_jsonable(rm(env).get_workflow(env.workflow_id))


def test_no_secret_keys(env):
    env.add_router()
    env.orchestrator().run_round(env.workflow_id)
    db = env.sf()
    job = db.scalar(select(PipelineJob))
    job.worker_id, job.claim_token = "worker-secret", "tok-secret"
    db.commit()
    db.close()
    payload = to_jsonable(rm(env).get_workflow(env.workflow_id))
    blob = json.dumps(payload)
    assert "worker-secret" not in blob and "tok-secret" not in blob
    for item in walk(payload):
        if isinstance(item, str):
            assert "claim_token" not in item and "worker_id" not in item and "lease" not in item


def test_error_message_bounded_and_path_scrubbed(env):
    env.add_router()
    env.orchestrator().tick(env.workflow_id)
    db = env.sf()
    job = db.scalar(select(PipelineJob))
    job.status, job.last_error_code = "failed", "task_failed"
    job.last_error_message = "failed at C:\\Users\\bob\\x.txt and /home/bob/y " + "z" * 900
    db.commit()
    db.close()
    j = rm(env).get_project(env.project_id).steps[1].job
    assert len(j.last_error_message) <= 400 and "bob" not in j.last_error_message


# --- filters / pagination / not found -----------------------------------------------------


def test_list_filters_and_pagination(env, session_factory):
    db = session_factory()
    ids = []
    for i in range(4):
        wf = ChannelWorkflow(name=f"w{i}", mode="auto", status="draft" if i % 2 else "active",
                             created_at=NOW + timedelta(minutes=i + 1))
        db.add(wf)
        db.commit()
        ids.append(wf.id)
    db.close()
    r = rm(env)
    everything = r.list_workflows()
    assert len(everything) == 5 and [w.name for w in everything] == ["chan", "w3", "w2", "w1", "w0"]
    assert [w.name for w in r.list_workflows(limit=2)] == ["chan", "w3"]
    assert [w.name for w in r.list_workflows(limit=2, offset=3)] == ["w1", "w0"]
    assert {w.status for w in r.list_workflows(status="draft")} == {"draft"}
    assert len(r.list_workflows(status=ChannelWorkflowStatus.ACTIVE)) == 3
    assert r.list_workflows(offset=50) == []
    with pytest.raises(ValidationFailed):
        r.list_workflows(status="bogus")
    with pytest.raises(ValidationFailed):
        r.list_workflows(limit=-1)


def test_not_found_codes(env):
    r = rm(env)
    for call in (lambda: r.get_workflow("nope"), lambda: r.get_project("nope"), lambda: r.get_runner("nope"),
                 lambda: r.list_runners(workflow_id="nope")):
        with pytest.raises(NotFound) as ei:
            call()
        assert ei.value.code == "not_found"


# --- runners -----------------------------------------------------------------------------


def test_runner_snapshots_and_effective_state(env):
    db = env.sf()
    rows = {
        "free": RunnerInstance(runner_type="a_free", external_id="e1", supported_roles=ROLES),
        "full": RunnerInstance(runner_type="b_full", max_concurrency=2, active_count=2),
        "quota": RunnerInstance(runner_type="c_quota", state="quota_exhausted",
                                quota_reset_at=NOW + timedelta(hours=1)),
        "quota_past": RunnerInstance(runner_type="d_qp", state="quota_exhausted",
                                     quota_reset_at=NOW - timedelta(seconds=1)),
        "cool": RunnerInstance(runner_type="e_cool", state="cooldown", cooldown_until=NOW + timedelta(minutes=5)),
        "cool_past": RunnerInstance(runner_type="f_cp", state="rate_limited", cooldown_until=NOW),
        "off": RunnerInstance(runner_type="g_off", state="offline"),
        "dis": RunnerInstance(runner_type="h_dis", enabled=False),
        "auth": RunnerInstance(runner_type="i_auth", state="auth_error", error_message="x" * 900),
    }
    for r in rows.values():
        r.workflow_session_id = None
        db.add(r)
    db.commit()
    ids = {k: r.id for k, r in rows.items()}
    db.close()
    r = rm(env)
    eff = {k: r.get_runner(i).effective_state for k, i in ids.items()}
    assert eff == {"free": "ready", "full": "busy", "quota": "quota_exhausted", "quota_past": "ready",
                   "cool": "cooldown", "cool_past": "ready", "off": "offline", "dis": "disabled",
                   "auth": "auth_error"}
    free = r.get_runner(ids["free"])
    assert free.external_id == "e1" and not free.assigned and free.free_slots == 1
    assert r.get_runner(ids["full"]).free_slots == 0
    assert len(r.get_runner(ids["auth"]).error_message) == 400
    unassigned = r.list_runners(unassigned=True)
    assert {x.id for x in unassigned} == set(ids.values())  # foreign runner is assigned, excluded
    assigned = r.list_runners(session_id=env.other_session_id)
    assert len(assigned) == 1 and assigned[0].assigned and assigned[0].workflow_session_id == env.other_session_id
    assert r.list_runners(workflow_id=env.workflow_id) == []
    env.add_router()
    assert len(r.list_runners(workflow_id=env.workflow_id)) == 1
    with pytest.raises(ValidationFailed):
        r.list_runners(unassigned=True, session_id=env.session_id)


def test_capacity_counts(env):
    db = env.sf()
    for kw in ({"state": "quota_exhausted", "quota_reset_at": NOW + timedelta(hours=1)},
               {"state": "cooldown", "cooldown_until": NOW + timedelta(hours=1)},
               {"state": "offline"}, {"active_count": 1}, {}):
        db.add(RunnerInstance(workflow_session_id=env.session_id, runner_type="x", supported_roles=ROLES, **kw))
    db.commit()
    db.close()
    cap = rm(env).get_workflow(env.workflow_id).capacity
    assert (cap.registered, cap.ready, cap.busy, cap.offline, cap.quota, cap.cooldown) == (5, 1, 1, 1, 1, 1)
    assert {r.role: r.capable for r in cap.roles} == {"story_writer": 5, "tts_adapter": 5}


# --- small units --------------------------------------------------------------------------


def test_categorize_and_to_jsonable():
    assert categorize_error("INFRA_EXHAUSTED") == "infrastructure" == categorize_error("LEASE_EXPIRED")
    assert categorize_error("runner_crashed") == "infrastructure"
    assert categorize_error("task_failed") == categorize_error("invalid_output") == "business"
    assert categorize_error("ALL_AGENTS_UNAVAILABLE") == "capacity"
    assert categorize_error("provider_blocked") == "provider"
    assert categorize_error("weird") == categorize_error(None) == "unknown"
    from enum import Enum

    class E(Enum):
        A = "a"

    assert to_jsonable({"x": (1, E.A), "t": NOW, 3: {2}}) == {"x": [1, "a"], "t": NOW.isoformat(), "3": [2]}
    with pytest.raises(TypeError):
        to_jsonable(object())
    assert effective_runner_state(RunnerInstance(state="ready", enabled=True, active_count=0, max_concurrency=1),
                                  NOW) == "ready"
