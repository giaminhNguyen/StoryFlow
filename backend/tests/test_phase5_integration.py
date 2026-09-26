"""Phase 5 integration: control plane (services + read models + runtime) over the real
alembic-migrated schema with deterministic fakes.

Scenarios required by the roadmap: lifecycle, cancellation race (late runner result), two
independent runtime instances against one SQLite file. No sleeps; threads only with
Events/Barriers; frozen clock.
"""

import threading
from datetime import datetime

import pytest
from sqlalchemy import func, select

from storyflow.agents import AgentRunner
from storyflow.errors import InvalidState
from storyflow.models import (
    AudioChunk, AudioGeneration, CanonAnalysis, ChannelWorkflow, JobStatus, PipelineJob, StoryGeneration,
    StoryVersion, TTSGeneration,
)
from storyflow.readmodels import ReadModels
from storyflow.roles import Role
from storyflow.runtime import StaticRunnerProvider, build_runtime
from storyflow.runtime.app import PipelineRouter
from storyflow.services import RunnerService, WorkflowService
from storyflow.subtitles import FakeSubtitleClient

NOW = datetime(2026, 3, 1, 9, 0, 0)
TRACK = {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True,
         "snippets": [{"text": "The hero wakes.", "start": 0.0}, {"text": "The rival waits.", "start": 2.0}]}
ROLES = [Role.STORY_WRITER.value, Role.TTS_ADAPTER.value]
CONFIG = {"source": {"video_id": "vid", "languages": ["en"]}, "story": {"branch": "a darker turn"},
          "tts": {"voice": "narrator"}}


class Stack:
    """One 'process' + its control-plane services, sharing a migrated DB file."""

    def __init__(self, tmp_path, *, router=None, first=True):
        self.tmp_path = tmp_path
        db_url = f"sqlite:///{(tmp_path / 'storyflow.db').as_posix()}"
        root = tmp_path / "artifacts"
        from storyflow.artifacts import ArtifactStore
        store = ArtifactStore(root)
        self.router = router or PipelineRouter(store)
        provider = StaticRunnerProvider({"r1": self.router}, name="fake", roles=ROLES)
        self.app = build_runtime(
            database_url=db_url, artifact_root=root, providers=[provider], clock=lambda: NOW,
            subtitle_client=FakeSubtitleClient({"vid": {"tracks": [TRACK]}}),
            ensure_db_schema=first, sleep=lambda seconds: None)
        self.workflows = WorkflowService(self.app.ctx, self.app.orchestrator)
        self.runners = RunnerService(self.app.session_factory, self.app.ctx.clock)
        self.read = ReadModels(self.app.ctx, self.app.orchestrator.chain)

    def new_workflow(self, name="chan"):
        wf = self.workflows.create_workflow(name, config=CONFIG).workflow_id
        self.workflows.add_project(wf, "Tale")
        return wf

    def assign_discovered_runner(self, workflow_id):
        self.app.supervisor.refresh()
        (runner,) = self.read.list_runners(unassigned=True)
        return self.runners.assign_runner(runner.id, workflow_id=workflow_id)

    def drive(self, workflow_id, until, *, max_iterations=60):
        for _ in range(max_iterations):
            self.app.runtime.run_once()
            snap = self.read.get_workflow(workflow_id)
            if until(snap):
                return snap
        raise AssertionError(f"condition not reached; last state {snap.display_state}")

    def count(self, model, *where):
        with self.app.session_factory() as db:
            return db.scalar(select(func.count()).select_from(model).where(*where))


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    yield s
    s.app.close()


# --- lifecycle -----------------------------------------------------------------


def test_lifecycle_create_start_pause_resume_failure_retry_completed(stack):
    wf = stack.workflows.create_workflow("empty", config=CONFIG).workflow_id
    with pytest.raises(InvalidState) as empty:
        stack.workflows.start(wf)
    assert empty.value.details["reason"] == "no_projects"

    wf = stack.new_workflow()
    assert stack.read.get_workflow(wf).display_state == "draft"
    stack.workflows.start(wf)

    # a runner is discovered but NOT granted to the session: work parks, nothing runs
    stack.drive(wf, lambda s: s.display_state == "waiting_capacity")
    assert [r.assigned for r in stack.read.list_runners()] == [False]
    assert stack.count(PipelineJob, PipelineJob.status == JobStatus.WAITING_CAPACITY.value) == 1

    stack.assign_discovered_runner(wf)
    stack.workflows.pause(wf)
    paused = stack.read.get_workflow(wf)
    assert paused.display_state == "paused" and paused.status_reason == "operator"
    before = stack.count(PipelineJob)
    stack.app.runtime.run_once()  # paused workflows are not served
    assert stack.count(PipelineJob) == before
    assert stack.read.get_workflow(wf).display_state == "paused"

    # story generation will fail (invalid output => business failure, bounded retries)
    stack.router.story.emit_invalid = {"story"}
    stack.workflows.resume(wf)
    failed = stack.drive(wf, lambda s: s.display_state == "failed")
    project = failed.projects[0]
    assert project.current_step == "story" and project.failure.category == "business"
    assert project.failure.attempts == project.failure.max_attempts
    assert stack.count(StoryVersion) == 0
    with pytest.raises(InvalidState) as resume_failed:
        stack.workflows.resume(wf)
    assert resume_failed.value.details["reason"] == "has_failed_steps"

    stack.router.story.emit_invalid = False  # provider recovered
    stack.workflows.retry(wf)
    done = stack.drive(wf, lambda s: s.display_state == "completed")
    assert done.counts.completed == 1
    project = done.projects[0]
    assert project.story_version.version_number == 1 and project.audio.chunk_count == len(project.audio.chunks) > 0
    assert stack.count(StoryGeneration, StoryGeneration.status == "completed") == 1
    assert stack.count(StoryGeneration, StoryGeneration.status == "failed") == 1
    assert stack.count(StoryVersion) == 1 and stack.count(TTSGeneration) == 1
    assert stack.count(AudioGeneration) == 1


# --- cancellation race -----------------------------------------------------------


class BlockingRunner(AgentRunner):
    """Delegates to the fake router but holds the first canon task until released, so the
    workflow can be cancelled while the runner is mid-flight."""

    def __init__(self, inner):
        self.inner = inner
        self.runner_type = inner.runner_type
        self.started = threading.Event()
        self.release = threading.Event()

    def execute(self, packet):
        if packet.task_config["step"] == "canon":
            self.started.set()
            assert self.release.wait(timeout=30), "test never released the runner"
        return self.inner.execute(packet)

    def classify_error(self, error):
        return self.inner.classify_error(error)


def test_cancel_while_runner_processing_late_result_cannot_resurrect(tmp_path):
    from storyflow.artifacts import ArtifactStore
    router = BlockingRunner(PipelineRouter(ArtifactStore(tmp_path / "artifacts")))
    stack = Stack(tmp_path, router=router)
    try:
        wf = stack.new_workflow()
        stack.workflows.start(wf)
        stack.assign_discovered_runner(wf)
        worker = threading.Thread(target=stack.app.runtime.run_once)
        worker.start()
        assert router.started.wait(timeout=30)          # runner is processing the canon job
        result = stack.workflows.cancel(wf)
        assert result.status == "cancelled" and result.changed
        router.release.set()                            # ...and now returns late
        worker.join(timeout=30)
        assert not worker.is_alive()

        for _ in range(3):                              # nothing may advance afterwards
            stack.app.runtime.run_once()
        snap = stack.read.get_workflow(wf)
        assert snap.status == "cancelled" and snap.display_state == "cancelled"
        with stack.app.session_factory() as db:
            kinds = [(j.kind, j.status) for j in db.scalars(select(PipelineJob))]
            assert kinds == [("canon_analysis", JobStatus.COMPLETED.value)]  # late result recorded, nothing else
            assert [a.status for a in db.scalars(select(CanonAnalysis))] == ["cancelled"]
        assert stack.count(StoryGeneration) == 0 and stack.count(StoryVersion) == 0
        assert stack.workflows.cancel(wf).changed is False  # idempotent
    finally:
        router.release.set()
        stack.app.close()


# --- two independent runtimes ------------------------------------------------------


def test_two_runtime_instances_never_duplicate_work(tmp_path):
    first = Stack(tmp_path, first=True)
    second = Stack(tmp_path, first=False)
    try:
        wf = first.new_workflow()
        first.workflows.start(wf)
        first.assign_discovered_runner(wf)
        second.app.supervisor.refresh()  # registers the same DB runner row in the 2nd process
        barrier = threading.Barrier(2)
        errors = []

        def loop(stack):
            barrier.wait(timeout=30)
            for _ in range(80):
                report = stack.app.runtime.run_once()
                errors.extend(report.errors)
                with stack.app.session_factory() as db:
                    if db.get(ChannelWorkflow, wf, populate_existing=True).status == "finished":
                        return

        threads = [threading.Thread(target=loop, args=(s,)) for s in (first, second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        assert not any(t.is_alive() for t in threads)

        assert first.read.get_workflow(wf).display_state == "completed", errors
        assert first.count(StoryGeneration) == 1 and first.count(StoryVersion) == 1
        assert first.count(TTSGeneration) == 1 and first.count(AudioGeneration) == 1
        assert first.count(AudioChunk) > 0
        with first.app.session_factory() as db:
            indexes = [c.chunk_index for c in db.scalars(select(AudioChunk))]
            assert len(indexes) == len(set(indexes))
            jobs = db.scalars(select(PipelineJob)).all()
            kinds = sorted(j.kind for j in jobs)
        assert kinds == ["audio_generation", "canon_analysis", "story_generation", "tts_generation"],             [(j.kind, j.status, j.dedupe_key, j.attempts, j.execution_count) for j in jobs]
        assert errors == []
    finally:
        first.app.close()
        second.app.close()
