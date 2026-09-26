"""Source-step failure policy (roadmap 4.2): backoff, retry limit, skip / needs_attention, and what the
orchestrator + read models do with a project that ended without pausing the workflow."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from storyflow.artifacts import ArtifactStore
from storyflow.errors import ValidationFailed
from storyflow.models import ChannelWorkflow, StoryProject
from storyflow.orchestrator import Orchestrator
from storyflow.pipeline import PipelineContext, StepStatus
from storyflow.policy import RECOMMENDED
from storyflow.readmodels import ReadModels
from storyflow.services import WorkflowService
from storyflow.story_steps import SourceStep
from storyflow.subtitles import (
    BlockedByProvider, FakeSubtitleClient, ProviderTimeout, ProviderUnavailable,
)

T0 = datetime(2026, 4, 1, 8, 0, 0)
TRACK = {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True,
         "snippets": [{"text": "one", "start": 0.0}, {"text": "two", "start": 1.0}]}


class Scripted(FakeSubtitleClient):
    """FakeSubtitleClient that first raises whatever is queued for a video, and counts provider calls."""

    def __init__(self, store, script=None):
        super().__init__(store)
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.calls = []

    def fetch(self, video_id, *args, **kwargs):
        self.calls.append(video_id)
        queued = self.script.get(video_id)
        if queued:
            exc = queued.pop(0)
            if exc is not None:
                raise exc
        return super().fetch(video_id, *args, **kwargs)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


def make_ctx(session_factory, store, clock, client):
    return PipelineContext(session_factory=session_factory, store=store, subtitle_client=client, clock=clock)


def make_project(db, policy=None, video="vid", title="P", workflow=None):
    cfg = {"source": {"video_id": video, "languages": ["en"]}}
    if policy is not None:
        cfg["failure_policy"] = policy
    if workflow is None:
        workflow = ChannelWorkflow(name="c", mode="auto", status="active", config=cfg)
        db.add(workflow)
        db.commit()
    p = StoryProject(title=title, channel_workflow_id=workflow.id)
    db.add(p)
    db.commit()
    return p


def fresh(db, model, pk):
    return db.get(model, pk, populate_existing=True)


BACKOFF = {"subtitle_retries": 3, "retry_base_seconds": 30, "retry_max_seconds": 900}


# --- transient errors: backoff + retry limit ------------------------------------------------


def test_transient_error_backs_off_without_calling_the_provider(db, session_factory, store, clock):
    client = Scripted({"vid": {"tracks": [TRACK]}}, {"vid": [BlockedByProvider("429")]})
    ctx = make_ctx(session_factory, store, clock, client)
    project = make_project(db, BACKOFF)

    view = SourceStep().run(ctx, project.id)
    assert (view.status, view.error_code) == (StepStatus.NOT_STARTED, "provider_blocked")
    p = fresh(db, StoryProject, project.id)
    assert p.source_attempts == 1 and p.next_attempt_at == T0 + timedelta(seconds=30)
    assert p.status == "active" and p.status_detail["last_error"] == "provider_blocked"

    clock.advance(29)  # still inside the backoff window: the provider must not be touched
    view = SourceStep().run(ctx, project.id)
    assert (view.status, view.error_code) == (StepStatus.NOT_STARTED, "provider_blocked")
    assert client.calls == ["vid"]

    clock.advance(2)  # window over: the retry succeeds and clears the retry state
    view = SourceStep().run(ctx, project.id)
    assert view.status is StepStatus.COMPLETED and client.calls == ["vid", "vid"]
    p = fresh(db, StoryProject, project.id)
    assert p.source_attempts == 0 and p.next_attempt_at is None and p.status_detail is None


def test_backoff_doubles_each_attempt(db, session_factory, store, clock):
    errors = [ProviderTimeout("t"), BlockedByProvider("b")]
    client = Scripted({"vid": {"tracks": [TRACK]}}, {"vid": errors})
    ctx = make_ctx(session_factory, store, clock, client)
    project = make_project(db, {**BACKOFF, "subtitle_retries": 5})

    assert SourceStep().run(ctx, project.id).error_code == "provider_timeout"
    assert fresh(db, StoryProject, project.id).next_attempt_at == T0 + timedelta(seconds=30)
    clock.advance(30)
    assert SourceStep().run(ctx, project.id).error_code == "provider_blocked"
    assert fresh(db, StoryProject, project.id).next_attempt_at == clock.now + timedelta(seconds=60)


def test_retries_exhausted_pauses_by_default(db, session_factory, store, clock):
    client = Scripted({"vid": {"tracks": [TRACK]}}, {"vid": [BlockedByProvider("x")] * 5})
    ctx = make_ctx(session_factory, store, clock, client)
    project = make_project(db, BACKOFF)
    codes = []
    for _ in range(3):
        codes.append(SourceStep().run(ctx, project.id).error_code)
        clock.advance(10_000)
    assert codes == ["provider_blocked", "provider_blocked", "subtitle_retries_exhausted"]
    p = fresh(db, StoryProject, project.id)
    assert p.status == "active" and p.status_reason is None  # legacy: the workflow pauses, the project stays active


def test_retries_exhausted_ends_only_this_project_under_continue(db, session_factory, store, clock):
    client = Scripted({"vid": {"tracks": [TRACK]}}, {"vid": [BlockedByProvider("x")] * 5})
    ctx = make_ctx(session_factory, store, clock, client)
    project = make_project(db, {**BACKOFF, "on_permanent_error": "continue"})
    for _ in range(3):
        view = SourceStep().run(ctx, project.id)
        clock.advance(10_000)
    assert view.status is StepStatus.FAILED and view.error_code == "subtitle_retries_exhausted"
    p = fresh(db, StoryProject, project.id)
    assert p.status == "needs_attention" and p.status_reason == "subtitle_retries_exhausted"
    assert p.next_attempt_at is None
    calls = len(client.calls)
    assert SourceStep().run(ctx, project.id).error_code == "subtitle_retries_exhausted"  # terminal: no provider call
    assert len(client.calls) == calls


def test_legacy_policy_retries_on_every_run_forever(db, session_factory, store, clock):
    client = Scripted({"vid": {"tracks": [TRACK]}}, {"vid": [BlockedByProvider("x")] * 20})
    ctx = make_ctx(session_factory, store, clock, client)
    project = make_project(db)  # no failure_policy at all
    for _ in range(10):
        assert SourceStep().run(ctx, project.id).status is StepStatus.NOT_STARTED
    assert len(client.calls) == 10 and fresh(db, StoryProject, project.id).next_attempt_at is None


# --- no subtitle / permanent / operator ------------------------------------------------------


@pytest.mark.parametrize("video,code", [("missing", "subtitles_unavailable"), ("nolang", "language_unavailable")])
def test_no_subtitle_is_skipped_under_skip_policy(db, session_factory, store, clock, video, code):
    client = Scripted({"nolang": {"tracks": [{**TRACK, "language_code": "fr", "is_translatable": False}]}})
    ctx = make_ctx(session_factory, store, clock, client)
    project = make_project(db, {"on_no_subtitle": "skip"}, video=video)
    view = SourceStep().run(ctx, project.id)
    assert (view.status, view.error_code) == (StepStatus.FAILED, code)
    p = fresh(db, StoryProject, project.id)
    assert p.status == "skipped" and p.status_reason == code
    assert p.status_detail == {"step": "source", "error_code": code}


def test_no_subtitle_pauses_by_default(db, session_factory, store, clock):
    ctx = make_ctx(session_factory, store, clock, Scripted({}))
    project = make_project(db, None, video="missing")
    assert SourceStep().run(ctx, project.id).error_code == "subtitles_unavailable"
    assert fresh(db, StoryProject, project.id).status == "active"


def test_operator_error_never_ends_a_project(db, session_factory, store, clock):
    client = Scripted({"vid": {"tracks": [TRACK]}}, {"vid": [ProviderUnavailable("no deps")]})
    ctx = make_ctx(session_factory, store, clock, client)
    project = make_project(db, {"on_no_subtitle": "skip", "on_permanent_error": "continue"})
    view = SourceStep().run(ctx, project.id)
    assert (view.status, view.error_code) == (StepStatus.FAILED, "provider_unavailable")
    assert fresh(db, StoryProject, project.id).status == "active"


def test_missing_source_config_is_a_permanent_error(db, session_factory, store, clock):
    ctx = make_ctx(session_factory, store, clock, Scripted({}))
    wf = ChannelWorkflow(name="c", mode="auto", status="active", config={"failure_policy": {"on_permanent_error": "continue"}})
    db.add(wf)
    db.commit()
    project = make_project(db, workflow=wf)
    assert SourceStep().run(ctx, project.id).error_code == "source_not_configured"
    assert fresh(db, StoryProject, project.id).status == "needs_attention"


# --- orchestrator: one bad item must not stop the batch -------------------------------------


def make_batch(db, policy):
    wf = ChannelWorkflow(name="batch", mode="auto", status="active", config={
        "source": {"video_id": "vid", "languages": ["en"]}, "failure_policy": policy})
    db.add(wf)
    db.commit()
    good = StoryProject(title="good", channel_workflow_id=wf.id, created_at=T0)  # tick order = creation order
    bad = StoryProject(title="bad", channel_workflow_id=wf.id, created_at=T0 + timedelta(seconds=1))
    db.add_all([good, bad])
    db.commit()
    return wf, good, bad


def source_only_orchestrator(ctx):
    return Orchestrator(ctx, dispatcher=None, source=SourceStep(), steps=[])


def test_skip_policy_lets_the_rest_of_the_batch_finish(db, session_factory, store, clock):
    from storyflow.subtitles import SubtitlesUnavailable
    client = Scripted({"vid": {"tracks": [TRACK]}}, {"vid": [None, SubtitlesUnavailable("gone")]})
    ctx = make_ctx(session_factory, store, clock, client)
    wf, good, bad = make_batch(db, {"on_no_subtitle": "skip"})
    orch = source_only_orchestrator(ctx)

    res = orch.tick(wf.id)
    assert res.workflow_status == "finished"           # not paused: every project is done or ended
    assert res.failed == [] and [(p, s) for p, s, _ in res.ended] == [(bad.id, "source")]
    assert res.projects[good.id].step is None and res.projects[good.id].terminal is None
    assert res.projects[bad.id].terminal == "skipped" and res.projects[bad.id].error_code == "subtitles_unavailable"
    assert fresh(db, StoryProject, good.id).status == "completed" and fresh(db, StoryProject, bad.id).status == "skipped"


def test_pause_policy_keeps_the_legacy_behaviour(db, session_factory, store, clock):
    from storyflow.subtitles import SubtitlesUnavailable
    client = Scripted({"vid": {"tracks": [TRACK]}}, {"vid": [None, SubtitlesUnavailable("gone")]})
    ctx = make_ctx(session_factory, store, clock, client)
    wf, good, bad = make_batch(db, {"on_no_subtitle": "pause"})
    res = source_only_orchestrator(ctx).tick(wf.id)
    assert res.workflow_status == "paused" and res.failed == [(bad.id, "source", "subtitles_unavailable")]
    detail = fresh(db, ChannelWorkflow, wf.id).status_detail
    assert detail["project_id"] == bad.id and detail["error_code"] == "subtitles_unavailable"


def test_all_projects_skipped_still_finishes_the_workflow(db, session_factory, store, clock):
    ctx = make_ctx(session_factory, store, clock, Scripted({}))  # nothing known: every video has no subtitle
    wf, good, bad = make_batch(db, {"on_no_subtitle": "skip"})
    res = source_only_orchestrator(ctx).tick(wf.id)
    assert res.workflow_status == "finished" and len(res.ended) == 2


def test_ended_project_is_not_touched_again(db, session_factory, store, clock):
    client = Scripted({})
    ctx = make_ctx(session_factory, store, clock, client)
    wf, good, bad = make_batch(db, {"on_no_subtitle": "skip"})
    orch = source_only_orchestrator(ctx)
    orch.tick(wf.id)
    calls = len(client.calls)
    orch.tick(wf.id)
    assert len(client.calls) == calls


def test_backoff_wait_does_not_pause_or_fail_the_workflow(db, session_factory, store, clock):
    client = Scripted({"vid": {"tracks": [TRACK]}}, {"vid": [BlockedByProvider("429")]})
    ctx = make_ctx(session_factory, store, clock, client)
    wf = ChannelWorkflow(name="c", mode="auto", status="active", config={
        "source": {"video_id": "vid", "languages": ["en"]}, "failure_policy": BACKOFF})
    db.add(wf)
    db.commit()
    project = make_project(db, workflow=wf)
    orch = source_only_orchestrator(ctx)
    assert orch.tick(wf.id).workflow_status == "active"     # blocked -> waiting
    clock.advance(5)
    assert orch.tick(wf.id).workflow_status == "active" and client.calls == ["vid"]
    clock.advance(30)
    assert orch.tick(wf.id).workflow_status == "finished" and client.calls == ["vid", "vid"]


# --- read models ------------------------------------------------------------------------------


def test_read_model_reports_skipped_needs_attention_and_pending_retry(db, session_factory, store, clock):
    from storyflow.subtitles import SubtitlesUnavailable
    client = Scripted({"vid": {"tracks": [TRACK]}}, {"vid": [None, SubtitlesUnavailable("gone"),
                                                              BlockedByProvider("429")]})
    ctx = make_ctx(session_factory, store, clock, client)
    wf, good, bad = make_batch(db, {**BACKOFF, "on_no_subtitle": "skip"})
    waiting = StoryProject(title="waiting", channel_workflow_id=wf.id, created_at=T0 + timedelta(seconds=2))
    db.add(waiting)
    db.commit()
    orch = source_only_orchestrator(ctx)
    orch.tick(wf.id)
    read = ReadModels(ctx, orch.chain)

    snap = read.get_workflow(wf.id)
    states = {p.title: p for p in snap.projects}
    assert states["good"].state == "completed"
    assert states["bad"].state == "skipped" and states["bad"].status == "skipped"
    assert states["bad"].status_reason == "subtitles_unavailable" and states["bad"].failure is None
    assert states["waiting"].state == "not_started" and states["waiting"].source_attempts == 1
    assert states["waiting"].block.kind == "delayed" and states["waiting"].block.until == T0 + timedelta(seconds=30)
    assert snap.counts.skipped == 1 and snap.counts.completed == 1 and snap.counts.needs_attention == 0
    assert snap.display_state == "active"


# --- create_workflow validation ---------------------------------------------------------------


@pytest.fixture
def workflows(session_factory, store, clock):
    ctx = make_ctx(session_factory, store, clock, Scripted({}))
    return WorkflowService(ctx, source_only_orchestrator(ctx))


def test_create_workflow_records_the_recommended_policy_by_default(workflows, db):
    wf_id = workflows.create_workflow("w", config={"source": {"video_id": "x"}}).workflow_id
    assert fresh(db, ChannelWorkflow, wf_id).config["failure_policy"] == RECOMMENDED


def test_create_workflow_merges_an_explicit_policy_over_the_recommended_values(workflows, db):
    policy = {"on_no_subtitle": "skip"}
    wf_id = workflows.create_workflow("w", config={"failure_policy": policy}).workflow_id
    stored = fresh(db, ChannelWorkflow, wf_id).config["failure_policy"]
    assert stored == {**RECOMMENDED, "on_no_subtitle": "skip"}      # the given key wins, the rest keeps the backoff
    assert policy == {"on_no_subtitle": "skip"}                       # the caller's dict is not mutated
    opt_out = workflows.create_workflow("w2", config={"failure_policy": {"subtitle_retries": None}}).workflow_id
    assert fresh(db, ChannelWorkflow, opt_out).config["failure_policy"]["subtitle_retries"] is None


@pytest.mark.parametrize("bad", [{"on_no_subtitle": "explode"}, {"subtitle_retries": 0}, {"bogus": 1}, "skip"])
def test_create_workflow_rejects_an_invalid_policy(workflows, bad):
    with pytest.raises(ValidationFailed) as exc:
        workflows.create_workflow("w", config={"failure_policy": bad})
    assert exc.value.details["reason"] == "invalid_failure_policy"
    assert db_count(workflows) == 0


def db_count(workflows):
    with workflows.ctx.session_factory() as db:
        return len(db.scalars(select(ChannelWorkflow)).all())


def test_new_source_error_codes_have_a_failure_category():
    from storyflow.readmodels import categorize_error
    for code in ("subtitle_retries_exhausted", "provider_timeout", "provider_unavailable"):
        assert categorize_error(code) == "provider"
