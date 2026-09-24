"""Phase 4 story steps: SourceStep, CanonStep, StoryStep, validators and fake runner.

Deterministic: FakeSubtitleClient, tmp_path ArtifactStore, fixed clock, no sleeps. Job
semantics (business failure, attempts) are exercised through the real Dispatcher.
"""

import json
from datetime import datetime

import pytest
from sqlalchemy import select

from storyflow import queue
from storyflow.agents import CRASH, RunnerRegistry
from storyflow.artifacts import ArtifactStore
from storyflow.dispatcher import Dispatcher, DispatchOutcome
from storyflow.models import (
    CanonAnalysis, ChannelWorkflow, PipelineJob, RunnerAttempt, RunnerInstance, SourceSnapshot,
    StoryGeneration, StoryProject, StoryVersion, WorkflowSession,
)
from storyflow.pipeline import OutputValidatingRunner, PipelineContext, StepStatus
from storyflow.protocol import ResultCode, TaskPacket
from storyflow.story_steps import (
    STORY_VALIDATORS, CanonStep, FakeStoryPipelineRunner, SourceStep, StoryStep,
    canon_path, skill_pin, story_path, validate_canon,
)
from storyflow.subtitles import FakeSubtitleClient

NOW = datetime(2026, 1, 2, 12, 0, 0)
TRACK = {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True,
         "snippets": [{"text": "line one", "start": 0.0}, {"text": "line two", "start": 1.0}]}


def client(**entries):
    return FakeSubtitleClient({"vid": {"tracks": [TRACK]}, **entries})


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def make_ctx(session_factory, store):
    def _make(sub=None):
        return PipelineContext(session_factory=session_factory, store=store,
                               subtitle_client=sub or client(), clock=lambda: NOW)
    return _make


@pytest.fixture
def ctx(make_ctx):
    return make_ctx()


@pytest.fixture
def project(db):
    wf = ChannelWorkflow(name="c", mode="auto", status="active", config={
        "source": {"video_id": "vid", "languages": ["en"]},
        "story": {"branch": "what if", "direction": "dark", "target_length": 500}})
    db.add(wf)
    db.commit()
    p = StoryProject(title="My story", channel_workflow_id=wf.id)
    db.add(p)
    db.commit()
    return p


def fresh(db, model, pk):
    return db.get(model, pk, populate_existing=True)


def make_snapshot(ctx, project):
    view = SourceStep().run(ctx, project.id)
    assert view.status is StepStatus.COMPLETED
    return view.domain_id


def run_job(db, session, runner, spec, now_kw=None, **enqueue_kw):
    """Enqueue a JobSpec and drive it through one Dispatcher round."""
    job = queue.enqueue_job(db, kind=spec.kind, payload=spec.payload, dedupe_key=spec.dedupe_key,
                            priority=spec.priority, role=spec.role, session_id=session.id, **enqueue_kw)
    outcome, _ = Dispatcher(reg_for(db, session, runner)).run_round(db, session)
    return job, outcome


def reg_for(db, session, runner):
    inst = db.scalar(select(RunnerInstance).where(RunnerInstance.workflow_session_id == session.id))
    if inst is None:
        inst = RunnerInstance(workflow_session_id=session.id, runner_type=runner.runner_type,
                              max_concurrency=2, supported_roles=["story_writer"])
        db.add(inst)
        db.commit()
    reg = RunnerRegistry()
    reg.register(inst.id, runner)
    return reg


@pytest.fixture
def session(db):
    s = WorkflowSession(mode="auto", status="active")
    db.add(s)
    db.commit()
    return s


def reload_job(db, job_id):
    return db.scalar(select(PipelineJob).where(PipelineJob.id == job_id).execution_options(populate_existing=True))


# --- source -------------------------------------------------------------------


def test_source_creates_snapshot_with_relative_artifact_and_provenance(db, ctx, project, store):
    view = SourceStep().run(ctx, project.id)
    assert view.status is StepStatus.COMPLETED
    snap = fresh(db, SourceSnapshot, view.domain_id)
    assert snap.snapshot_number == 1 and snap.content == "line one\nline two"
    assert len(snap.content_hash) == 64
    rel = snap.meta["artifact_path"]
    assert rel == f"projects/{project.id}/source/0001/source.txt"
    assert store.read(rel).decode() == snap.content
    assert snap.meta["video_id"] == "vid" and snap.meta["language_code"] == "en"
    assert snap.meta["is_generated"] is False and snap.meta["translated"] is False
    assert snap.meta["provider"] == "FakeSubtitleClient"
    assert SourceStep().status(db, ctx, project).status is StepStatus.COMPLETED


def test_source_run_is_idempotent_and_snapshot_immutable(db, make_ctx, project):
    first = SourceStep().run(make_ctx(), project.id)
    changed = client(vid={"tracks": [dict(TRACK, snippets=[{"text": "different", "start": 0.0}])]})
    second = SourceStep().run(make_ctx(changed), project.id)
    assert second.domain_id == first.domain_id
    rows = db.scalars(select(SourceSnapshot)).all()
    assert len(rows) == 1 and rows[0].content == "line one\nline two"


def test_source_concurrent_runs_converge_on_one_snapshot(db, session_factory, store, project):
    step = SourceStep()
    views = []

    class Reentrant(FakeSubtitleClient):
        """While run A is inside fetch (no txn open), run B completes fully."""
        def fetch(self, *a, **kw):
            result = super().fetch(*a, **kw)
            if not views:
                views.append(step.run(PipelineContext(session_factory, store, Plain, lambda: NOW), project.id))
            return result

    Plain = client()
    view_a = step.run(PipelineContext(session_factory, store, Reentrant({"vid": {"tracks": [TRACK]}}),
                                      lambda: NOW), project.id)
    assert view_a.domain_id == views[0].domain_id
    assert len(db.scalars(select(SourceSnapshot)).all()) == 1


def test_source_unique_index_conflict_returns_none_without_raising(ctx, project):
    make_snapshot(ctx, project)
    dup = SourceStep._insert_snapshot(ctx, project.id, 1, "t", "x", "h", {})
    assert dup is None


@pytest.mark.parametrize("error,status,code", [
    ("no_subtitle", StepStatus.FAILED, "subtitles_unavailable"),
    ("blocked", StepStatus.NOT_STARTED, "provider_blocked"),
])
def test_source_provider_errors(db, make_ctx, project, error, status, code):
    view = SourceStep().run(make_ctx(client(vid={"tracks": [TRACK], "error": error})), project.id)
    assert (view.status, view.error_code) == (status, code)
    assert db.scalars(select(SourceSnapshot)).all() == []


def test_source_language_unavailable_is_failed(db, make_ctx, project):
    wf = fresh(db, ChannelWorkflow, project.channel_workflow_id)
    wf.config = {"source": {"video_id": "vid", "languages": ["fr"], "allow_translation": False}}
    db.commit()
    view = SourceStep().run(make_ctx(), project.id)
    assert (view.status, view.error_code) == (StepStatus.FAILED, "language_unavailable")


def test_source_blocked_then_retry_succeeds(db, make_ctx, project):
    assert SourceStep().run(make_ctx(client(vid={"tracks": [TRACK], "error": "blocked"})), project.id
                            ).status is StepStatus.NOT_STARTED
    assert SourceStep().run(make_ctx(), project.id).status is StepStatus.COMPLETED


def test_source_missing_config_is_failed(db, make_ctx):
    p = StoryProject(title="bare")
    db.add(p)
    db.commit()
    view = SourceStep().run(make_ctx(), p.id)
    assert (view.status, view.error_code) == (StepStatus.FAILED, "source_not_configured")


# --- canon --------------------------------------------------------------------


def wrapped(store, **kw):
    return OutputValidatingRunner(FakeStoryPipelineRunner(store, **kw), store, STORY_VALIDATORS)


def test_canon_begin_payload_and_conflict(db, ctx, project):
    snap_id = make_snapshot(ctx, project)
    step = CanonStep()
    assert step.status(db, ctx, project).status is StepStatus.NOT_STARTED
    aid, spec = step.begin(db, ctx, project)
    aid2, spec2 = step.begin(db, ctx, project)
    assert aid == aid2 and spec.dedupe_key == f"canon:{aid}"
    assert spec.kind == "canon_analysis" and spec.role == "story_writer"
    p = spec.payload
    assert p["skill"] == skill_pin() and p["skill"]["path"] == "skills/story-branch-writer"
    assert len(p["skill"]["revision"]) == 40
    assert p["inputs"] == {"source_artifact": f"projects/{project.id}/source/0001/source.txt",
                           "snapshot_id": snap_id, "project_id": project.id}
    assert p["outputs"] == [f"projects/{project.id}/canon/{aid}/canon.json"]
    assert p["task_config"]["step"] == "canon"
    assert step.status(db, ctx, project).status is StepStatus.IN_PROGRESS
    assert len(db.scalars(select(CanonAnalysis)).all()) == 1


def test_canon_requires_snapshot(db, ctx, project):
    assert CanonStep().begin(db, ctx, project) is None


def test_canon_full_path_through_dispatcher_and_finalize_idempotent(db, ctx, project, store, session):
    make_snapshot(ctx, project)
    step = CanonStep()
    aid, spec = step.begin(db, ctx, project)
    job, outcome = run_job(db, session, wrapped(store), spec)
    assert outcome is DispatchOutcome.DISPATCHED_SUCCESS
    step.link_job(db, ctx, aid, job)
    assert fresh(db, CanonAnalysis, aid).pipeline_job_id == job.id
    step.finalize(db, ctx, aid, reload_job(db, job.id))
    step.finalize(db, ctx, aid, reload_job(db, job.id))
    row = fresh(db, CanonAnalysis, aid)
    assert row.status == "completed" and row.finished_at == NOW and validate_canon(row.canon) is None
    assert step.status(db, ctx, project).status is StepStatus.COMPLETED
    assert step.begin(db, ctx, project) is None  # nothing left to begin


def test_canon_invalid_file_fails_analysis_when_finalized_directly(db, ctx, project, store):
    make_snapshot(ctx, project)
    step = CanonStep()
    aid, spec = step.begin(db, ctx, project)
    store.write(spec.payload["outputs"][0], json.dumps({"version": 1}).encode())
    step.finalize(db, ctx, aid, None)
    row = fresh(db, CanonAnalysis, aid)
    assert row.status == "failed" and row.error_code == "invalid_canon" and row.canon is None
    assert step.status(db, ctx, project).status is StepStatus.FAILED
    assert step.begin(db, ctx, project)[0] != aid  # explicit begin creates a fresh analysis


def test_canon_mark_failed_and_missing_output(db, ctx, project):
    make_snapshot(ctx, project)
    step = CanonStep()
    aid, _ = step.begin(db, ctx, project)
    step.finalize(db, ctx, aid, None)
    assert fresh(db, CanonAnalysis, aid).error_code == "missing_output"
    aid2, _ = step.begin(db, ctx, project)
    job = PipelineJob(kind="canon_analysis", last_error_code="task_failed", last_error_message="boom")
    step.mark_failed(db, ctx, aid2, job)
    assert (fresh(db, CanonAnalysis, aid2).status, fresh(db, CanonAnalysis, aid2).error_code) == ("failed", "task_failed")


def test_validate_canon_rejects_bad_shapes():
    assert validate_canon({}) is not None
    assert validate_canon([]) is not None
    base = {"version": 1, "central_conflict": "c",
            "characters": [{"id": "a", "name": "A", "function": "f"}],
            "relationships": [{"from": "a", "to": "zzz", "type": "t"}],
            "events": [{"id": "e", "summary": "s"}],
            "leverage_points": [{"id": "l", "description": "d"}]}
    assert "unknown character" in validate_canon(base)
    base["relationships"] = []
    assert validate_canon(base) is None
    base["events"] = []
    assert "events" in validate_canon(base)


# --- story --------------------------------------------------------------------


def complete_canon(db, ctx, project, store, session):
    make_snapshot(ctx, project)
    step = CanonStep()
    aid, spec = step.begin(db, ctx, project)
    job, _ = run_job(db, session, wrapped(store), spec)
    step.finalize(db, ctx, aid, job)
    return aid


def test_story_begin_requires_completed_canon(db, ctx, project):
    assert StoryStep().begin(db, ctx, project) is None
    make_snapshot(ctx, project)
    assert StoryStep().begin(db, ctx, project) is None
    CanonStep().begin(db, ctx, project)  # canon queued, not completed
    assert StoryStep().begin(db, ctx, project) is None


def test_story_begin_payload_and_conflict(db, ctx, project, store, session):
    canon_id = complete_canon(db, ctx, project, store, session)
    step = StoryStep()
    gid, spec = step.begin(db, ctx, project)
    assert step.begin(db, ctx, project)[0] == gid
    gen = fresh(db, StoryGeneration, gid)
    assert gen.trigger == "pipeline" and gen.canon_analysis_id == canon_id and gen.status == "queued"
    assert gen.config == {"branch": "what if", "direction": "dark", "target_length": 500}
    assert spec.kind == "story_generation" and spec.dedupe_key == f"story:{gid}" and spec.role == "story_writer"
    assert spec.payload["outputs"] == [story_path(project.id, gid)]
    assert spec.payload["inputs"]["canon_artifact"] == canon_path(project.id, canon_id)
    assert spec.payload["inputs"]["branch"] == "what if"
    assert spec.payload["task_config"]["step"] == "story"
    assert len(db.scalars(select(StoryGeneration)).all()) == 1


def test_story_finalize_creates_immutable_version_and_is_idempotent(db, ctx, project, store, session):
    complete_canon(db, ctx, project, store, session)
    step = StoryStep()
    gid, spec = step.begin(db, ctx, project)
    job, outcome = run_job(db, session, wrapped(store), spec)
    assert outcome is DispatchOutcome.DISPATCHED_SUCCESS
    step.finalize(db, ctx, gid, job)
    step.finalize(db, ctx, gid, job)  # retry / double delivery
    versions = db.scalars(select(StoryVersion)).all()
    assert len(versions) == 1
    v = versions[0]
    assert v.story_generation_id == gid and v.version_number == 1 and v.word_count > 0
    assert v.content_path == story_path(project.id, gid) and v.content == store.read(v.content_path).decode()
    assert fresh(db, StoryGeneration, gid).status == "completed"
    # a later finalize (even after generation completion) never touches the version
    content = v.content
    store.write(v.content_path, b"tampered later output with enough words here")
    step.finalize(db, ctx, gid, job)
    assert fresh(db, StoryVersion, v.id).content == content
    assert step.status(db, ctx, project).status is StepStatus.COMPLETED
    assert step.begin(db, ctx, project) is None


def test_story_version_number_is_next_per_project(db, ctx, project, store, session):
    complete_canon(db, ctx, project, store, session)
    db.add(StoryVersion(story_project_id=project.id, version_number=4, title="old", content="old"))
    db.commit()
    step = StoryStep()
    gid, spec = step.begin(db, ctx, project)
    job, _ = run_job(db, session, wrapped(store), spec)
    step.finalize(db, ctx, gid, job)
    assert db.scalar(select(StoryVersion).where(StoryVersion.story_generation_id == gid)).version_number == 5


def test_story_invalid_output_through_dispatcher_is_business_failure(db, ctx, project, store, session):
    complete_canon(db, ctx, project, store, session)
    step = StoryStep()
    gid, spec = step.begin(db, ctx, project)
    runner = FakeStoryPipelineRunner(store, emit_invalid={"story"})
    job, outcome = run_job(db, session, OutputValidatingRunner(runner, store, STORY_VALIDATORS), spec)
    assert outcome is DispatchOutcome.DISPATCHED_REQUEUED
    j = reload_job(db, job.id)
    assert j.attempts == 1 and j.status == "queued" and j.infrastructure_failures == 0
    attempt = db.scalars(select(RunnerAttempt).where(RunnerAttempt.pipeline_job_id == job.id)).one()
    assert attempt.result_type == "invalid_output"
    assert db.scalars(select(StoryVersion)).all() == []
    assert fresh(db, StoryGeneration, gid).status == "queued"
    assert step.status(db, ctx, project).status is StepStatus.IN_PROGRESS


def test_story_terminal_failure_then_explicit_begin_creates_fresh_generation(db, ctx, project, store, session):
    complete_canon(db, ctx, project, store, session)
    step = StoryStep()
    gid, spec = step.begin(db, ctx, project)
    bad = OutputValidatingRunner(FakeStoryPipelineRunner(store, emit_invalid=True), store, STORY_VALIDATORS)
    job, _ = run_job(db, session, bad, spec, max_attempts=1)
    j = reload_job(db, job.id)
    assert j.status == "failed"
    step.mark_failed(db, ctx, gid, j)
    step.mark_failed(db, ctx, gid, j)
    view = step.status(db, ctx, project)
    assert (view.status, view.error_code) == (StepStatus.FAILED, "invalid_output")
    assert db.scalars(select(StoryVersion)).all() == []
    gid2, spec2 = step.begin(db, ctx, project)
    assert gid2 != gid and spec2.dedupe_key != spec.dedupe_key
    # a completed generation wins over the older failed one
    job2, _ = run_job(db, session, wrapped(store), spec2)
    step.finalize(db, ctx, gid2, job2)
    view = step.status(db, ctx, project)
    assert (view.status, view.domain_id) == (StepStatus.COMPLETED, gid2)


def test_story_finalize_rejects_bad_content_without_version(db, ctx, project, store, session):
    complete_canon(db, ctx, project, store, session)
    step = StoryStep()
    for content, code in [(b"\xff\xfe\xfa", "invalid_story"), (b"   \n", "invalid_story"),
                          (f"a b c d e see {store.root}/x".encode(), "invalid_story"),
                          (b"a b c d e see C:\\Users\\x\\secret", "invalid_story")]:
        gid, spec = step.begin(db, ctx, project)
        store.write(spec.payload["outputs"][0], content)
        step.finalize(db, ctx, gid, None)
        assert fresh(db, StoryGeneration, gid).error_code == code
    assert db.scalars(select(StoryVersion)).all() == []


def test_story_finalize_number_conflict_does_not_raise(db, ctx, project, store, session, monkeypatch):
    complete_canon(db, ctx, project, store, session)
    step = StoryStep()
    gid, spec = step.begin(db, ctx, project)
    job, _ = run_job(db, session, wrapped(store), spec)
    # a competing writer takes version 1 between our read of the max and our insert
    db.add(StoryVersion(story_project_id=project.id, version_number=1, title="rival", content="rival"))
    db.commit()
    import storyflow.story_steps as mod
    real = mod._next_version_number
    calls = []

    def stale(db_, pid):
        calls.append(1)
        return 1 if len(calls) == 1 else real(db_, pid)  # first read is stale -> conflicts with rival
    monkeypatch.setattr(mod, "_next_version_number", stale)
    step.finalize(db, ctx, gid, job)
    mine = db.scalars(select(StoryVersion).where(StoryVersion.story_generation_id == gid)).all()
    assert len(mine) == 1 and mine[0].version_number == 2
    assert fresh(db, StoryGeneration, gid).status == "completed"


# --- validators / fake runner / paths ------------------------------------------


def packet(project_id, outputs, step="story", **inputs):
    return TaskPacket(inputs={"project_id": project_id, **inputs}, outputs=outputs, task_config={"step": step})


@pytest.mark.parametrize("bad", ["../evil/story.md", "/abs/story.md", "C:\\x\\story.md",
                                 "projects/other/story/g/story.md", "projects/P/story/../../story.md",
                                 "projects/P/story/g/other.md"])
def test_output_paths_are_validated(store, bad):
    pkt = packet("P", [bad])
    assert STORY_VALIDATORS["story"](pkt, store) is not None
    runner = FakeStoryPipelineRunner(store)
    res = runner.execute(packet("P", [bad], canon_artifact="x"))
    assert res.code is ResultCode.TASK_FAILED and res.error_code == "bad_output_path"
    assert not any(p.name == "story.md" for p in store.root.rglob("*") if p.is_file())


def test_fake_runner_scripted_results_and_crash(store, db, session):
    runner = FakeStoryPipelineRunner(store, results=[ResultCode.TASK_FAILED, CRASH])
    pkt = packet("P", ["projects/P/story/g/story.md"])
    assert runner.execute(pkt).code is ResultCode.TASK_FAILED
    with pytest.raises(RuntimeError):
        runner.execute(pkt)
    assert len(runner.invocations) == 2 and not list(store.root.rglob("story.md"))


def test_no_absolute_paths_persisted(db, ctx, project, store, session, tmp_path):
    complete_canon(db, ctx, project, store, session)
    step = StoryStep()
    gid, spec = step.begin(db, ctx, project)
    job, _ = run_job(db, session, wrapped(store), spec)
    step.finalize(db, ctx, gid, job)
    root = str(tmp_path)
    blobs = [json.dumps(s.meta) for s in db.scalars(select(SourceSnapshot))]
    blobs += [v.content_path for v in db.scalars(select(StoryVersion))]
    blobs += [json.dumps(j.payload_json) for j in db.scalars(select(PipelineJob))]
    blobs += [json.dumps(c.canon) for c in db.scalars(select(CanonAnalysis))]
    assert blobs and all(root not in b and "\\" not in b for b in blobs)
    for s in db.scalars(select(SourceSnapshot)):
        assert not s.meta["artifact_path"].startswith(("/", "C:"))
