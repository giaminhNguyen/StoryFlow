"""Batch behaviour end to end (roadmap 4.3 / 4.6 / 4.8): the ``batch.max_active`` window, and a poisoned item
that must not stop the rest of the batch.

Real SourceStep / Canon / Story / TTS / Audio handlers, the real dispatcher and the deterministic fake runners
(same ``Stack`` as test_phase5_integration). Rounds are driven one at a time so the window invariant is checked
after EVERY round, not only at the end. No sleeps; the clock is only moved where a test says so.
"""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from storyflow.artifacts import ArtifactStore
from storyflow.models import ChannelWorkflow, PipelineJob, SourceSnapshot, StoryProject
from storyflow.protocol import ResultCode, RunnerResult
from storyflow.runtime.app import PipelineRouter
from storyflow.source_service import SourceService
from storyflow.subtitles import BlockedByProvider, FakeSubtitleClient
from test_phase5_integration import NOW, TRACK, Stack

IDS = [c * 11 for c in "ABCDEFGH"]          # valid 11-character video ids
MISSING = "M" * 11                            # a video without any subtitle
MISSING2 = "N" * 11
CLOSED = ("completed", "skipped", "needs_attention")
STEPS = ("canon", "story", "tts", "audio")


class PoisonRouter(PipelineRouter):
    """The fake pipeline runner, except that chosen (project, step) pairs fail for good."""

    def __init__(self, store):
        super().__init__(store)
        self.poison: dict[str, set[str]] = {}

    def execute(self, packet):
        step = packet.task_config["step"]
        project_id = packet.outputs[0].split("/")[1] if packet.outputs else None
        if step in self.poison.get(project_id, ()):
            return RunnerResult(code=ResultCode.TASK_FAILED, error_code="poisoned", error_message="poisoned item")
        return super().execute(packet)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)


class BatchStack(Stack):
    def __init__(self, tmp_path, videos, *, batch=None, policy=None, subtitles=None, start=True):
        router = PoisonRouter(ArtifactStore(tmp_path / "artifacts"))
        super().__init__(tmp_path, router=router)
        self.subtitles = subtitles or FakeSubtitleClient({v: {"tracks": [TRACK]} for v in IDS})
        self.app.ctx.subtitle_client = self.subtitles
        config = {"source": {"languages": ["en"]}, "tts": {"voice": "narrator"},
                  "failure_policy": policy if policy is not None else {},
                  "batch": batch if batch is not None else {"max_active": None}}
        self.wf = self.workflows.create_workflow("batch", config=config).workflow_id
        added = SourceService(self.app.ctx).add_sources(self.wf, list(videos)).added
        self.pids = [a["project_id"] for a in added]
        assert len(self.pids) == len(videos)
        self.orch = self.app.orchestrator
        if start:
            self.workflows.start(self.wf)
            self.assign_discovered_runner(self.wf)
        # observations
        self.max_open = 0
        self.start_order: list[str] = []
        self.closed_when_started: dict[str, int] = {}

    # --- driving ---------------------------------------------------------------------------

    def round(self):
        rnd = self.orch.run_round(self.wf)
        snap = self.read.get_workflow(self.wf)
        self.observe(snap)
        return snap

    def run(self, until, *, rounds=500):
        for _ in range(rounds):
            snap = self.round()
            if until(snap):
                return snap
            if snap.status not in ("active",):
                break
        raise AssertionError(f"condition not reached; workflow {snap.status}/{snap.display_state} "
                             f"{[p.state for p in snap.projects]}")

    def observe(self, snap):
        closed = sum(1 for p in snap.projects if p.state in CLOSED)
        open_ids = [p.id for p in snap.projects if p.source is not None and p.state not in CLOSED]
        self.max_open = max(self.max_open, len(open_ids))
        for p in snap.projects:
            if p.source is not None and p.id not in self.start_order:
                self.start_order.append(p.id)
                self.closed_when_started[p.id] = closed

    # --- helpers ---------------------------------------------------------------------------

    def project(self, snap, index):
        return next(p for p in snap.projects if p.id == self.pids[index])

    def finished(self, snap):
        return snap.status == "finished"


@pytest.fixture
def make_stack(tmp_path):
    made = []

    def make(videos, **kw):
        s = BatchStack(tmp_path, videos, **kw)
        made.append(s)
        return s

    yield make
    for s in made:
        s.app.close()


# --- the window ---------------------------------------------------------------------------------


def test_window_never_runs_more_than_max_active_and_starts_in_creation_order(make_stack):
    s = make_stack(IDS[:5], batch={"max_active": 2})
    snap = s.run(s.finished)
    assert s.max_open == 2                       # they DO overlap (2 at once) ...
    assert s.start_order == s.pids               # ... always oldest first ...
    assert [p.state for p in snap.projects] == ["completed"] * 5 and snap.display_state == "completed"
    # ... and a slot only frees up when a project is done: when project k starts, at most one earlier one is open
    assert [s.closed_when_started[p] >= max(0, k - 1) for k, p in enumerate(s.pids)] == [True] * 5
    assert s.closed_when_started[s.pids[0]] == s.closed_when_started[s.pids[1]] == 0


def test_a_window_of_one_processes_the_batch_strictly_one_at_a_time(make_stack):
    s = make_stack(IDS[:3], batch={"max_active": 1})
    s.run(s.finished)
    assert s.max_open == 1 and s.start_order == s.pids
    assert [s.closed_when_started[p] for p in s.pids] == [0, 1, 2]


def test_no_window_starts_every_project_at_once(make_stack):
    s = make_stack(IDS[:5], batch={"max_active": None})
    s.round()
    snap = s.read.get_workflow(s.wf)
    assert sum(1 for p in snap.projects if p.source is not None) == 5      # legacy: everything is advanced
    s.run(s.finished)
    assert s.max_open == 5


def test_projects_beyond_the_window_have_no_side_effects_yet(make_stack):
    s = make_stack(IDS[:4], batch={"max_active": 1})
    s.round()
    with s.app.session_factory() as db:
        snapshots = db.scalars(select(SourceSnapshot.story_project_id)).all()
        assert snapshots == [s.pids[0]]                                    # no subtitle fetch for the waiting ones
        jobs = db.scalars(select(PipelineJob)).all()
        assert jobs and {j.payload_json["inputs"]["project_id"] for j in jobs} == {s.pids[0]}   # only its own jobs


def test_run_until_idle_completes_a_windowed_batch(make_stack):
    s = make_stack(IDS[:5], batch={"max_active": 2})
    out = s.orch.run_until_idle(s.wf, max_rounds=500)
    assert out.status == "completed" and out.workflow_status == "finished"


def test_a_skipped_video_frees_its_slot_within_the_same_tick(make_stack):
    subs = FakeSubtitleClient({v: {"tracks": [TRACK]} for v in IDS})     # MISSING / MISSING2 are unknown: no subtitle
    s = make_stack([MISSING, MISSING2, IDS[0], IDS[1]], batch={"max_active": 1},
                   policy={"on_no_subtitle": "skip"}, subtitles=subs)
    tick = s.orch.tick(s.wf)
    with s.app.session_factory() as db:
        statuses = {p.video_id: p.status for p in db.scalars(select(StoryProject))}
        started = set(db.scalars(select(SourceSnapshot.story_project_id)).all())
    assert statuses[MISSING] == statuses[MISSING2] == "skipped"
    assert started == {s.pids[2]}                                          # the next good video took the slot now
    assert [e[0] for e in tick.ended] == s.pids[:2] and tick.workflow_status == "active"
    snap = s.run(s.finished)
    assert [p.state for p in snap.projects] == ["skipped", "skipped", "completed", "completed"]
    assert snap.counts.skipped == 2 and snap.counts.completed == 2


def test_every_video_skipped_finishes_the_workflow(make_stack):
    s = make_stack([MISSING, MISSING2], batch={"max_active": 1}, policy={"on_no_subtitle": "skip"},
                   subtitles=FakeSubtitleClient({}))
    tick = s.orch.tick(s.wf)
    assert tick.workflow_status == "finished" and len(tick.ended) == 2


def test_a_retry_exhausted_project_frees_the_window(make_stack):
    class Blocking(FakeSubtitleClient):
        def fetch(self, video_id, *args, **kwargs):
            if video_id == IDS[0]:
                raise BlockedByProvider("429")
            return super().fetch(video_id, *args, **kwargs)

    subs = Blocking({v: {"tracks": [TRACK]} for v in IDS})
    policy = {"subtitle_retries": 2, "retry_base_seconds": 30, "retry_max_seconds": 900,
              "on_permanent_error": "continue"}
    s = make_stack(IDS[:2], batch={"max_active": 1}, policy=policy, subtitles=subs)
    clock = Clock()
    s.app.ctx.clock = clock
    snap = s.round()
    assert s.project(snap, 0).block.kind == "delayed" and s.project(snap, 1).source is None   # slot is held
    clock.advance(40)
    snap = s.run(s.finished)
    first, second = s.project(snap, 0), s.project(snap, 1)
    assert (first.state, first.status_reason) == ("needs_attention", "subtitle_retries_exhausted")
    assert second.state == "completed" and snap.display_state == "completed"


# --- a poisoned item ----------------------------------------------------------------------------


@pytest.mark.parametrize("step", STEPS)
def test_a_poisoned_item_ends_only_that_project_under_continue(make_stack, step):
    s = make_stack(IDS[:3], batch={"max_active": 2}, policy={"on_permanent_error": "continue"})
    s.router.poison[s.pids[1]] = {step}
    seen = set()
    snap = s.run(lambda sn: (seen.add(sn.status), s.finished(sn))[1])
    assert seen == {"active", "finished"}                                   # it never paused
    good_a, bad, good_c = (s.project(snap, i) for i in range(3))
    assert good_a.state == good_c.state == "completed"
    assert (bad.state, bad.status) == ("needs_attention", "needs_attention")
    assert bad.status_reason == "poisoned" and bad.status_detail == {"step": step, "error_code": "poisoned"}
    assert bad.failure is not None and bad.failure.step == step and bad.failure.attempts == bad.failure.max_attempts
    assert snap.counts.needs_attention == 1 and snap.counts.completed == 2
    assert snap.display_state == "completed" and snap.status_reason is None


def test_a_poisoned_item_pauses_the_workflow_under_the_legacy_policy(make_stack):
    s = make_stack(IDS[:3], batch={"max_active": 2}, policy={"on_permanent_error": "pause"})
    s.router.poison[s.pids[1]] = {"story"}
    snap = s.run(lambda sn: sn.status == "paused")
    assert snap.status_reason == "step_failed" and snap.display_state == "failed"
    assert snap.status_detail["project_id"] == s.pids[1] and snap.status_detail["step"] == "story"
    assert s.project(snap, 1).status == "active"                            # not ended: the operator retries it


def test_the_default_engine_policy_is_the_legacy_pause(make_stack):
    s = make_stack(IDS[:2], policy={})                                      # no failure_policy keys at all
    s.router.poison[s.pids[0]] = {"canon"}
    assert s.run(lambda sn: sn.status == "paused").status_reason == "step_failed"


def test_all_items_poisoned_still_finishes_the_workflow(make_stack):
    s = make_stack(IDS[:3], batch={"max_active": 2}, policy={"on_permanent_error": "continue"})
    for pid in s.pids:
        s.router.poison[pid] = {"story"}
    snap = s.run(s.finished)
    assert [p.state for p in snap.projects] == ["needs_attention"] * 3 and snap.counts.needs_attention == 3


def test_a_poisoned_item_also_frees_its_window_slot(make_stack):
    s = make_stack(IDS[:4], batch={"max_active": 1}, policy={"on_permanent_error": "continue"})
    s.router.poison[s.pids[0]] = {"canon"}
    snap = s.run(s.finished)
    assert [p.state for p in snap.projects] == ["needs_attention", "completed", "completed", "completed"]
    assert s.max_open == 1 and s.start_order == s.pids


# --- reactivation ---------------------------------------------------------------------------------


def test_an_ended_job_step_can_be_reactivated_and_then_completes(make_stack):
    s = make_stack(IDS[:2], batch={"max_active": 2}, policy={"on_permanent_error": "continue"})
    s.router.poison[s.pids[0]] = {"tts"}
    snap = s.run(s.finished)
    assert s.project(snap, 0).state == "needs_attention" and snap.finished_at is not None

    s.router.poison.clear()                                                 # the cause is fixed
    assert s.orch.reactivate_project(s.pids[0]) == "tts"
    reopened = s.read.get_workflow(s.wf)
    assert reopened.status == "active" and reopened.finished_at is None      # a finished workflow re-opens
    assert s.project(reopened, 0).status == "active" and s.project(reopened, 0).status_reason is None

    snap = s.run(s.finished)
    assert [p.state for p in snap.projects] == ["completed", "completed"] and snap.finished_at is not None
    assert snap.counts.needs_attention == 0


def test_a_skipped_source_can_be_reactivated_once_the_subtitle_exists(make_stack):
    subs = FakeSubtitleClient({v: {"tracks": [TRACK]} for v in IDS})
    s = make_stack([MISSING, IDS[0]], batch={"max_active": 2}, policy={"on_no_subtitle": "skip"}, subtitles=subs)
    snap = s.run(s.finished)
    assert [p.state for p in snap.projects] == ["skipped", "completed"]
    subs.store[MISSING] = {"tracks": [TRACK]}                               # the video got captions
    assert s.orch.reactivate_project(s.pids[0]) == "source"
    snap = s.run(s.finished)
    assert [p.state for p in snap.projects] == ["completed", "completed"]


def test_reactivating_a_project_that_is_not_ended_is_a_noop(make_stack):
    s = make_stack(IDS[:1], batch={"max_active": 1})
    assert s.orch.reactivate_project(s.pids[0]) is None
    assert s.orch.reactivate_project("does-not-exist") is None
    assert s.run(s.finished).counts.completed == 1


def test_reactivation_resets_the_source_retry_state(make_stack):
    s = make_stack([MISSING], policy={"on_no_subtitle": "skip"}, subtitles=FakeSubtitleClient({}))
    s.run(s.finished)
    with s.app.session_factory() as db:
        p = db.get(StoryProject, s.pids[0])
        p.source_attempts, p.next_attempt_at = 3, NOW + timedelta(hours=1)
        db.commit()
    s.orch.reactivate_project(s.pids[0])
    with s.app.session_factory() as db:
        p = db.get(StoryProject, s.pids[0], populate_existing=True)
        assert (p.status, p.status_reason, p.status_detail, p.source_attempts, p.next_attempt_at) == (
            "active", None, None, 0, None)
        assert db.get(ChannelWorkflow, s.wf, populate_existing=True).status == "active"


# --- ended projects and operator commands ------------------------------------------------------


def _jobs_of(s, project_id):
    with s.app.session_factory() as db:
        return [j for j in db.scalars(select(PipelineJob)) if j.payload_json["inputs"].get("project_id") == project_id]


def test_resuming_a_workflow_never_re_arms_an_ended_project(make_stack):
    """pause -> resume walks every project for FAILED steps; an ended (needs_attention) project must be left alone."""
    s = make_stack(IDS[:2], batch={"max_active": 2}, policy={"on_permanent_error": "continue"})
    s.router.poison[s.pids[0]] = {"story"}
    s.run(lambda sn: s.project(sn, 0).state == "needs_attention")
    jobs_before = len(_jobs_of(s, s.pids[0]))
    s.workflows.pause(s.wf)
    s.workflows.resume(s.wf)
    assert s.read.get_workflow(s.wf).status == "active"
    assert len(_jobs_of(s, s.pids[0])) == jobs_before                       # no new domain row / job for the ended one
    snap = s.run(s.finished)
    assert s.project(snap, 0).state == "needs_attention" and s.project(snap, 1).state == "completed"


def test_retry_failed_step_ignores_an_ended_project(make_stack):
    s = make_stack(IDS[:1], policy={"on_permanent_error": "continue"})
    s.router.poison[s.pids[0]] = {"canon"}
    s.run(s.finished)
    before = len(_jobs_of(s, s.pids[0]))
    assert s.orch.retry_failed_step(s.pids[0]) is None
    assert len(_jobs_of(s, s.pids[0])) == before
    assert s.read.get_workflow(s.wf).status == "finished"


def test_reactivating_a_project_of_a_cancelled_workflow_is_refused(make_stack):
    s = make_stack(IDS[:2], policy={"on_permanent_error": "continue"})
    s.router.poison[s.pids[0]] = {"story"}
    s.run(lambda sn: s.project(sn, 0).state == "needs_attention")
    s.workflows.cancel(s.wf)
    jobs_before = len(_jobs_of(s, s.pids[0]))
    assert s.orch.reactivate_project(s.pids[0]) is None
    with s.app.session_factory() as db:
        project = db.get(StoryProject, s.pids[0], populate_existing=True)
        assert project.status == "needs_attention" and project.status_reason == "poisoned"
    assert len(_jobs_of(s, s.pids[0])) == jobs_before
    assert s.read.get_workflow(s.wf).status == "cancelled"


def test_reactivating_leaves_a_paused_workflow_paused(make_stack):
    s = make_stack(IDS[:2], batch={"max_active": 2}, policy={"on_permanent_error": "continue"})
    s.router.poison[s.pids[0]] = {"story"}
    s.run(lambda sn: s.project(sn, 0).state == "needs_attention")
    s.workflows.pause(s.wf)
    s.router.poison.clear()
    assert s.orch.reactivate_project(s.pids[0]) == "story"
    assert s.read.get_workflow(s.wf).status == "paused"                     # the operator decides when to resume
    s.workflows.resume(s.wf)
    snap = s.run(s.finished)
    assert [p.state for p in snap.projects] == ["completed", "completed"]
