"""Phase 4 shared contract between the orchestrator and the Story/TTS step handlers.

Ownership
---------
* ``orchestrator.py``  (workstream 1): generic loop -- reads durable state, asks handlers what
  to do, enqueues PipelineJobs, drives the Phase 2 Dispatcher, reconciles finished jobs.
* ``story_steps.py``   (workstream 2): SourceStep, CanonStep, StoryStep handlers.
* ``tts_steps.py``     (workstream 3): TTSStep, AudioStep handlers.
This module holds only the types they share. No business logic.

Data flow (per StoryProject; each arrow is a durable DB fact, never in-memory state)
------------------------------------------------------------------------------------
  source (inline, SubtitleClient) -> SourceSnapshot
  canon   (job, role story_writer)  -> CanonAnalysis
  story   (job, role story_writer)  -> StoryGeneration -> StoryVersion
  tts     (job, role tts_adapter)   -> TTSGeneration (adapted text + chunk texts)
  audio   (job, role tts_adapter)   -> AudioGeneration -> AudioChunk[]
  all completed -> ChannelWorkflow.status = finished

Step protocol
-------------
The domain row is the durable *intent*; the PipelineJob is the *execution*.
``begin`` inserts the domain row (unique-index guarded -> concurrent callers converge on one
row), the orchestrator then enqueues the job by ``JobSpec.dedupe_key`` (queue.enqueue_job is
idempotent while active) and calls ``link_job``. Every stage is safe to repeat, so a crash
between any two stages is repaired by the next tick: a domain row that is queued but has no
linked job simply gets its job enqueued.

Runner execution
----------------
Handlers never call runners. They describe work as ``JobSpec.payload`` which the Phase 2
``build_task_packet`` turns into a TaskPacket:
    {"skill": {"name","path","revision"}, "inputs": {...}, "outputs": [relative artifact paths],
     "task_config": {"step": <handler step name>, ...}}
Payloads are persisted in pipeline_jobs.payload_json, so they hold RELATIVE artifact paths
only (never absolute machine paths). ``workspace_root`` is intentionally not set: runners
that touch files receive an ArtifactStore at construction (see ``OutputValidatingRunner``).

Business validation of runner output happens INSIDE the dispatcher path by registering
``OutputValidatingRunner`` around the real/fake runner: invalid output becomes
ResultCode.INVALID_OUTPUT, which Phase 2 already maps to business_failure (attempts+1,
bounded retry). ``finalize`` therefore only ever sees valid, complete output.

Artifacts live under ``projects/<story_project_id>/...`` in the ArtifactStore; the DB keeps
relative paths only. A completed artifact is never overwritten by a retry.
"""

import abc
import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from sqlalchemy import select

from .agents import AgentRunner
from .artifacts import ArtifactStore
from .models import ChannelWorkflow, PipelineJob, StoryProject
from .protocol import ResultCode, RunnerHealth, RunnerResult, TaskPacket
from .subtitles import SubtitleClient


class StepStatus(str, enum.Enum):
    NOT_STARTED = "not_started"   # no domain row for this step's current input
    IN_PROGRESS = "in_progress"   # domain row queued/processing (job may or may not exist yet)
    COMPLETED = "completed"       # domain row completed and its output is durable
    FAILED = "failed"             # domain row failed; needs an explicit retry (never auto-created)


@dataclass(frozen=True)
class StepView:
    status: StepStatus
    domain_id: str | None = None
    pipeline_job_id: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class JobSpec:
    kind: str                 # PipelineJob.kind, e.g. "canon_analysis"
    role: str                 # StoryFlow Role value; chosen here, never by the runner/provider
    dedupe_key: str           # stable: derived from domain ids only
    payload: dict = field(default_factory=dict)
    priority: int = 0


@dataclass
class PipelineContext:
    """Per-process collaborators. Nothing here is a source of truth about workflow progress."""

    session_factory: Callable
    store: ArtifactStore
    subtitle_client: SubtitleClient
    clock: Callable[[], datetime]
    # Optional collaborators of multi-source ingestion (roadmap 4.1); None = feature off.
    video_lister: object | None = None      # storyflow.sources.VideoLister (channel / playlist expansion)
    inbox_dir: object | None = None         # Path: operator-provided subtitle files (<video_id>.txt / .srt / .vtt)


class StepHandler(abc.ABC):
    """One job-backed pipeline step. All methods use short DB transactions and commit
    before returning; none may hold a transaction across a runner/subtitle call."""

    step: str = ""
    job_kind: str = ""
    role: str = ""

    @abc.abstractmethod
    def status(self, db, ctx: PipelineContext, project: StoryProject) -> StepView:
        """Pure read of durable state for this step's current input (missing input ->
        NOT_STARTED). Must not create rows."""

    @abc.abstractmethod
    def begin(self, db, ctx: PipelineContext, project: StoryProject) -> tuple[str, JobSpec] | None:
        """Idempotently create (or find) the queued domain row for this step and return
        (domain_id, JobSpec). Returns None if a prerequisite is missing. A unique-index
        conflict from a concurrent caller must resolve to the existing row, not raise."""

    @abc.abstractmethod
    def link_job(self, db, ctx: PipelineContext, domain_id: str, job: PipelineJob) -> None:
        """Set domain.pipeline_job_id = job.id (idempotent, guarded update)."""

    @abc.abstractmethod
    def finalize(self, db, ctx: PipelineContext, domain_id: str, job: PipelineJob) -> None:
        """Job COMPLETED -> persist derived domain rows from the artifacts and mark the domain
        row completed. Idempotent: calling twice must not duplicate versions/chunks/runs and
        must not overwrite completed output."""

    @abc.abstractmethod
    def mark_failed(self, db, ctx: PipelineContext, domain_id: str, job: PipelineJob) -> None:
        """Job FAILED/CANCELLED terminally -> domain row failed with the job's error code.
        Idempotent; never touches a completed row."""


class InlineStepHandler(abc.ABC):
    """A step executed in-process by the orchestrator instead of by a runner (source
    acquisition via SubtitleClient). Same rules: no DB transaction held during external I/O."""

    step: str = ""

    @abc.abstractmethod
    def status(self, db, ctx: PipelineContext, project: StoryProject) -> StepView: ...

    @abc.abstractmethod
    def run(self, ctx: PipelineContext, project_id: str) -> StepView:
        """Do the step (opening its own short sessions from ctx.session_factory) and return
        the resulting view. Idempotent: an existing completed result is returned unchanged.
        Provider failures (SubtitlesUnavailable, BlockedByProvider, LanguageUnavailable)
        must map to a view carrying an error_code, never an unhandled raise."""


# Validators receive the packet + store and return None when the output is valid, else a
# short error message. They must not mutate the DB.
OutputValidator = Callable[[TaskPacket, ArtifactStore], "str | None"]


class OutputValidatingRunner(AgentRunner):
    """Registry-side wrapper: run the inner runner, then validate its artifacts.

    Keeps invalid output inside the Phase 2 dispatcher path -- INVALID_OUTPUT is classified
    as a business failure there, so bounded retry semantics come for free. Validation is
    selected by ``packet.task_config["step"]``; a step without a validator passes through.
    """

    def __init__(self, inner: AgentRunner, store: ArtifactStore, validators: dict[str, OutputValidator]):
        self.inner = inner
        self.store = store
        self.validators = dict(validators)
        self.runner_type = inner.runner_type

    def health(self) -> RunnerHealth:
        return self.inner.health()

    def cancel(self, task_id: str) -> bool:
        return self.inner.cancel(task_id)

    def classify_error(self, error: BaseException) -> ResultCode:
        return self.inner.classify_error(error)

    def execute(self, packet: TaskPacket) -> RunnerResult:
        result = self.inner.execute(packet)
        if result.code is not ResultCode.SUCCESS:
            return result
        validator = self.validators.get((packet.task_config or {}).get("step"))
        if validator is None:
            return result
        problem = validator(packet, self.store)
        if problem is None:
            return result
        return RunnerResult(
            code=ResultCode.INVALID_OUTPUT, error_code="invalid_output",
            error_message=str(problem)[:400], metrics=result.metrics,
        )


def workflow_for_project(db, project: StoryProject) -> ChannelWorkflow | None:
    if project.channel_workflow_id is None:
        return None
    return db.scalar(select(ChannelWorkflow).where(ChannelWorkflow.id == project.channel_workflow_id))


def workflow_config(db, project: StoryProject) -> dict:
    """ChannelWorkflow.config schema consumed by Phase 4 (all keys optional except source):
      {"source": {"video_id": str, "languages": [str], "preference": "any", "allow_translation": true},
       "story":  {"branch": str|null, "direction": str|null, "target_length": int|null},
       "tts":    {"voice": str, "engine": str, "profile": str}}
    A project's own ``source_config`` (video_id, languages, ...) overrides the ``source`` block, so one workflow
    can hold many videos (see storyflow/sources.py and WorkflowService.add_sources).
    """
    wf = workflow_for_project(db, project)
    return dict(wf.config or {}) if wf is not None else {}
