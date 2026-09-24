"""Phase 5 read models: typed, JSON-friendly, strictly read-only views of operational state.

Future API/UI code consumes these frozen dataclasses (see ``to_jsonable``) and never ORM rows.

Guarantees
----------
* READ-ONLY: only SELECTs; no commit, no row creation, never calls a runner/gateway/dispatcher.
  Step state comes from ``handler.status`` (pure); ``begin``/``run``/``finalize`` are never used.
* DERIVED FROM THE DB ONLY: nothing is cached, so a fresh ``ReadModels`` on the same DB returns
  an equal snapshot (given the same ``ctx.clock()``).
* NO SECRETS: no claim_token, worker_id, lease info, DB URL or credentials. Artifact paths are
  the RELATIVE store paths (never the store root); a stored path that is absolute or contains
  ``..`` is dropped. Error messages are bounded to 400 chars and scrubbed of absolute paths.

Failure category mapping (``categorize_error``; case-insensitive)
-----------------------------------------------------------------
  business        task_failed, invalid_output, invalid_canon, invalid_story, missing_output,
                  partial_failure, chunks_missing, empty_source, source_not_configured,
                  project_not_found, job_failed, missing_input, bad_output_path, unknown_step
  infrastructure  INFRA_EXHAUSTED, LEASE_EXPIRED, runner_crashed, timeout, transient_failure,
                  rate_limited, quota_exhausted, auth_error
  capacity        ALL_AGENTS_UNAVAILABLE / all_agents_unavailable
  provider        provider_blocked, subtitles_unavailable, language_unavailable
  unknown         anything else (including a missing code)

display_state (workflow)
------------------------
  draft | active | paused | waiting_capacity | failed | blocked | cancelled | completed
  DRAFT->draft; CANCELLED/ABANDONED->cancelled; FINISHED->completed (blocked if a project is
  inconsistent / chunks_missing); PAUSED+step_failed->failed, other PAUSED->paused;
  ACTIVE: any project waiting on a waiting_capacity job -> waiting_capacity, else any project
  blocked (chunks_missing | provider_blocked | inconsistent) -> blocked, else active.
  A ``delayed`` block (job scheduled in the future) is informational and does not change it.

list_workflows approximation
----------------------------
``list_workflows`` never walks handlers. It uses a few aggregate queries per page (project
counts, "session has a waiting_capacity job", "an audio run flagged chunks_missing"). It equals
``get_workflow(...).display_state`` on all normal states; it does NOT detect (a) a FINISHED
workflow that is inconsistent with its steps, (b) provider_blocked, (c) a completed audio run
whose chunks are incomplete, and it attributes a waiting_capacity job to every workflow that
shares the session.
"""

import dataclasses
import enum
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

from sqlalchemy import distinct, func, select

from .errors import NotFound, ValidationFailed
from .models import (
    AudioChunk,
    AudioGeneration,
    CanonAnalysis,
    ChannelWorkflow,
    ChannelWorkflowStatus,
    DomainStatus,
    JobStatus,
    PauseReason,
    PipelineJob,
    RunnerInstance,
    RunnerState,
    SourceSnapshot,
    StoryGeneration,
    StoryProject,
    StoryVersion,
    TTSGeneration,
    VersionStatus,
)
from .pipeline import PipelineContext, StepStatus

_FRESH = {"execution_options": {"populate_existing": True}}
MESSAGE_LIMIT = 400
MAX_PAGE = 500

# ---------------------------------------------------------------------------- categories

BUSINESS, INFRASTRUCTURE, CAPACITY, PROVIDER, UNKNOWN = (
    "business", "infrastructure", "capacity", "provider", "unknown")

_CATEGORY_CODES = {
    BUSINESS: {"task_failed", "invalid_output", "invalid_canon", "invalid_story", "missing_output",
               "partial_failure", "chunks_missing", "empty_source", "source_not_configured",
               "project_not_found", "job_failed", "missing_input", "bad_output_path", "unknown_step"},
    INFRASTRUCTURE: {"infra_exhausted", "lease_expired", "runner_crashed", "timeout",
                     "transient_failure", "rate_limited", "quota_exhausted", "auth_error"},
    CAPACITY: {"all_agents_unavailable"},
    PROVIDER: {"provider_blocked", "subtitles_unavailable", "language_unavailable"},
}
_CODE_TO_CATEGORY = {code: cat for cat, codes in _CATEGORY_CODES.items() for code in codes}


def categorize_error(code: str | None) -> str:
    return _CODE_TO_CATEGORY.get((code or "").lower(), UNKNOWN)


BLOCK_WAITING_CAPACITY = "waiting_capacity"
BLOCK_CHUNKS_MISSING = "chunks_missing"
BLOCK_PROVIDER_BLOCKED = "provider_blocked"
BLOCK_DELAYED = "delayed"
BLOCK_INCONSISTENT = "inconsistent"
_BLOCKING_KINDS = (BLOCK_CHUNKS_MISSING, BLOCK_PROVIDER_BLOCKED, BLOCK_INCONSISTENT)

_STEP_DEDUPE_PREFIX = {"canon": "canon", "story": "story", "tts": "tts", "audio": "audio"}

# ---------------------------------------------------------------------------- sanitising

_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"']*")
_UNIX_PATH = re.compile(r"(?<![\w.])/(?:home|Users|tmp|var|etc|usr|mnt|root|opt|private)/[^\s\"']*")


def _bound(text: str | None, limit: int = MESSAGE_LIMIT) -> str | None:
    if text is None:
        return None
    text = _WIN_PATH.sub("<path>", str(text))
    text = _UNIX_PATH.sub("<path>", text)
    return text[:limit]


def _rel(path) -> str | None:
    """Return ``path`` only if it is a safe relative store path, else None."""
    if not isinstance(path, str) or not path:
        return None
    norm = path.replace("\\", "/")
    p = PurePosixPath(norm)
    if p.is_absolute() or ":" in norm or ".." in p.parts:
        return None
    return norm


# ---------------------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class JobSummary:
    id: str
    kind: str
    role: str
    status: str
    attempts: int
    max_attempts: int
    infrastructure_failures: int
    max_infra_attempts: int
    execution_count: int
    scheduled_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None
    outcome: str | None


@dataclass(frozen=True)
class StepSummary:
    step: str
    status: str
    domain_id: str | None
    error_code: str | None
    job: JobSummary | None


@dataclass(frozen=True)
class SourceSnapshotInfo:
    id: str
    snapshot_number: int
    title: str
    content_hash: str | None
    language: str | None
    language_code: str | None
    provenance: dict
    artifact_path: str | None


@dataclass(frozen=True)
class CanonInfo:
    id: str
    status: str
    has_canon: bool
    error_code: str | None


@dataclass(frozen=True)
class StoryGenerationInfo:
    id: str
    status: str
    trigger: str
    error_code: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class StoryVersionInfo:
    id: str
    version_number: int
    title: str
    word_count: int
    content_path: str | None


@dataclass(frozen=True)
class TTSInfo:
    id: str
    status: str
    voice: str
    engine: str
    chunk_count: int | None
    error_code: str | None


@dataclass(frozen=True)
class AudioChunkInfo:
    chunk_index: int
    artifact_path: str | None
    duration_ms: int


@dataclass(frozen=True)
class AudioInfo:
    id: str
    run_number: int
    status: str
    chunk_count: int
    store_dir: str | None
    error_code: str | None
    registered_chunks: int
    chunks: list = field(default_factory=list)   # list[AudioChunkInfo]


@dataclass(frozen=True)
class BlockInfo:
    kind: str
    message: str | None = None
    until: datetime | None = None


@dataclass(frozen=True)
class FailureInfo:
    category: str
    code: str | None
    message: str | None
    step: str | None
    attempts: int | None
    max_attempts: int | None
    infrastructure_failures: int | None
    max_infra_attempts: int | None


@dataclass(frozen=True)
class RunnerSnapshot:
    id: str
    runner_type: str
    external_id: str | None
    workflow_session_id: str | None
    assigned: bool
    enabled: bool
    state: str
    effective_state: str
    active_count: int
    max_concurrency: int
    free_slots: int
    supported_roles: list
    cooldown_until: datetime | None
    quota_reset_at: datetime | None
    last_health_at: datetime | None
    last_success_at: datetime | None
    error_code: str | None
    error_message: str | None


@dataclass(frozen=True)
class RoleCapacity:
    role: str
    capable: int          # runners of the session declaring the role
    eligible: int         # of those, effective_state == ready (a free slot right now)
    waiting_jobs: int     # jobs of the session parked in waiting_capacity for this role


@dataclass(frozen=True)
class CapacitySummary:
    registered: int
    ready: int
    busy: int
    offline: int          # offline + disabled + auth_error
    quota: int
    cooldown: int         # cooldown + rate_limited
    roles: list = field(default_factory=list)          # list[RoleCapacity]
    unserved_roles: list = field(default_factory=list)  # roles with waiting jobs and no eligible runner
    message: str | None = None


@dataclass(frozen=True)
class ProjectSnapshot:
    id: str
    workflow_id: str | None
    title: str
    slug: str | None
    status: str
    state: str            # completed|failed|blocked|waiting_capacity|in_progress|not_started
    current_step: str | None
    step_status: str | None
    steps: list = field(default_factory=list)   # list[StepSummary]
    source: SourceSnapshotInfo | None = None
    canon: CanonInfo | None = None
    story_generation: StoryGenerationInfo | None = None
    story_version: StoryVersionInfo | None = None
    tts: TTSInfo | None = None
    audio: AudioInfo | None = None
    block: BlockInfo | None = None
    failure: FailureInfo | None = None


@dataclass(frozen=True)
class WorkflowCounts:
    completed: int = 0
    failed: int = 0
    blocked: int = 0
    in_progress: int = 0
    waiting_capacity: int = 0
    not_started: int = 0


@dataclass(frozen=True)
class WorkflowSummary:
    id: str
    name: str
    mode: str
    status: str
    status_reason: str | None
    display_state: str
    project_count: int
    session_id: str | None
    created_at: datetime | None
    updated_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True)
class WorkflowSnapshot:
    id: str
    name: str
    mode: str
    status: str
    status_reason: str | None
    status_detail: dict | None
    display_state: str
    project_count: int
    counts: WorkflowCounts
    session_id: str | None
    created_at: datetime | None
    updated_at: datetime | None
    finished_at: datetime | None
    projects: list = field(default_factory=list)   # list[ProjectSnapshot]
    runners: list = field(default_factory=list)    # list[RunnerSnapshot]
    capacity: CapacitySummary | None = None


# ---------------------------------------------------------------------------- to_jsonable


def to_jsonable(obj):
    """Recursively convert read models to JSON-safe primitives (dataclass -> dict, Enum -> value,
    datetime -> ISO string, tuple/list/set -> list, dict keys -> str)."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj.value if isinstance(obj, enum.Enum) else obj
    if isinstance(obj, enum.Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(to_jsonable(k)): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((to_jsonable(v) for v in obj), key=str)
    raise TypeError(f"not JSON-able: {type(obj).__name__}")


# ---------------------------------------------------------------------------- runner state


def effective_runner_state(r: RunnerInstance, now: datetime) -> str:
    """Read-only: what the dispatcher would treat the runner as at ``now`` (expired quota /
    cooldown windows count as available). Never writes."""
    if not r.enabled:
        return RunnerState.DISABLED.value
    s = r.state
    if s in (RunnerState.OFFLINE.value, RunnerState.DISABLED.value, RunnerState.AUTH_ERROR.value):
        return s
    if s == RunnerState.QUOTA_EXHAUSTED.value:
        if r.quota_reset_at is None or now < r.quota_reset_at:
            return s
    elif s in (RunnerState.COOLDOWN.value, RunnerState.RATE_LIMITED.value):
        if r.cooldown_until is None or now < r.cooldown_until:
            return s
    if (r.active_count or 0) >= max(r.max_concurrency or 1, 1):
        return RunnerState.BUSY.value
    return RunnerState.READY.value


def _runner_snapshot(r: RunnerInstance, now: datetime) -> RunnerSnapshot:
    maxc = r.max_concurrency or 1
    active = r.active_count or 0
    return RunnerSnapshot(
        id=r.id, runner_type=r.runner_type, external_id=r.external_id,
        workflow_session_id=r.workflow_session_id, assigned=r.workflow_session_id is not None,
        enabled=bool(r.enabled), state=r.state, effective_state=effective_runner_state(r, now),
        active_count=active, max_concurrency=maxc, free_slots=max(maxc - active, 0),
        supported_roles=list(r.supported_roles or []), cooldown_until=r.cooldown_until,
        quota_reset_at=r.quota_reset_at, last_health_at=r.last_health_at,
        last_success_at=r.last_success_at, error_code=r.error_code,
        error_message=_bound(r.error_message))


def _capacity(runners: list, waiting_by_role: dict, *, has_session: bool) -> CapacitySummary:
    eff = [r.effective_state for r in runners]
    roles = set(waiting_by_role)
    for r in runners:
        roles.update(r.supported_roles)
    role_rows = []
    for role in sorted(roles):
        capable = [r for r in runners if role in r.supported_roles]
        role_rows.append(RoleCapacity(
            role=role, capable=len(capable),
            eligible=sum(1 for r in capable if r.effective_state == RunnerState.READY.value),
            waiting_jobs=waiting_by_role.get(role, 0)))
    unserved = [rc.role for rc in role_rows if rc.waiting_jobs and rc.eligible == 0]
    message = None
    if not has_session:
        message = "workflow has no runner session"
    elif not runners and waiting_by_role:
        message = "no runners are assigned to this workflow's session"
    elif unserved:
        message = "no eligible runner for role(s): " + ", ".join(unserved)
    return CapacitySummary(
        registered=len(runners), ready=eff.count(RunnerState.READY.value),
        busy=eff.count(RunnerState.BUSY.value),
        offline=sum(eff.count(s) for s in (RunnerState.OFFLINE.value, RunnerState.DISABLED.value,
                                           RunnerState.AUTH_ERROR.value)),
        quota=eff.count(RunnerState.QUOTA_EXHAUSTED.value),
        cooldown=eff.count(RunnerState.COOLDOWN.value) + eff.count(RunnerState.RATE_LIMITED.value),
        roles=role_rows, unserved_roles=unserved, message=message)


# ---------------------------------------------------------------------------- helpers

_PRIORITY = {DomainStatus.COMPLETED.value: 0, DomainStatus.QUEUED.value: 1,
             DomainStatus.PROCESSING.value: 1, DomainStatus.FAILED.value: 2}


def _pick(rows):
    """completed > live > failed/other, newest first within a class; None if empty."""
    if not rows:
        return None
    return sorted(rows, key=lambda r: (_PRIORITY.get(r.status, 3), -(r.created_at.timestamp() if r.created_at else 0),
                                       r.id))[0]


def _job_summary(j: PipelineJob | None) -> JobSummary | None:
    if j is None:
        return None
    return JobSummary(
        id=j.id, kind=j.kind, role=j.role, status=j.status, attempts=j.attempts or 0,
        max_attempts=j.max_attempts, infrastructure_failures=j.infrastructure_failures or 0,
        max_infra_attempts=j.max_infra_attempts, execution_count=j.execution_count or 0,
        scheduled_at=j.scheduled_at, last_error_code=j.last_error_code,
        last_error_message=_bound(j.last_error_message), outcome=j.outcome)


def _display_from(status: str, reason: str | None, project_states: list) -> str:
    """project_states: list of (state, block_kind) ."""
    if status == ChannelWorkflowStatus.DRAFT.value:
        return "draft"
    if status in (ChannelWorkflowStatus.CANCELLED.value, ChannelWorkflowStatus.ABANDONED.value):
        return "cancelled"
    kinds = [k for _, k in project_states]
    if status == ChannelWorkflowStatus.FINISHED.value:
        return "blocked" if any(k in _BLOCKING_KINDS for k in kinds) else "completed"
    if status == ChannelWorkflowStatus.PAUSED.value:
        return "failed" if reason == PauseReason.STEP_FAILED.value else "paused"
    if BLOCK_WAITING_CAPACITY in kinds:
        return "waiting_capacity"
    if any(k in _BLOCKING_KINDS for k in kinds):
        return "blocked"
    return "active"


# ---------------------------------------------------------------------------- ReadModels


class ReadModels:
    def __init__(self, ctx: PipelineContext, chain: list):
        self.ctx = ctx
        self.chain = list(chain)

    # ------------------------------------------------------------------ public API

    def list_workflows(self, *, status=None, limit: int = 100, offset: int = 0) -> list[WorkflowSummary]:
        if limit < 0 or offset < 0:
            raise ValidationFailed("limit and offset must be >= 0", limit=limit, offset=offset)
        limit = min(limit, MAX_PAGE)
        if isinstance(status, enum.Enum):
            status = status.value
        if status is not None and status not in {s.value for s in ChannelWorkflowStatus}:
            raise ValidationFailed("unknown workflow status", status=status)
        with self.ctx.session_factory() as db:
            q = select(ChannelWorkflow).order_by(ChannelWorkflow.created_at.desc(), ChannelWorkflow.id)
            if status is not None:
                q = q.where(ChannelWorkflow.status == status)
            wfs = db.scalars(q.limit(limit).offset(offset), **_FRESH).all()
            if not wfs:
                return []
            ids = [w.id for w in wfs]
            counts = dict(db.execute(
                select(StoryProject.channel_workflow_id, func.count())
                .where(StoryProject.channel_workflow_id.in_(ids))
                .group_by(StoryProject.channel_workflow_id)).all())
            active = [w for w in wfs if w.status == ChannelWorkflowStatus.ACTIVE.value]
            waiting_sessions: set = set()
            chunks_missing: set = set()
            if active:
                sessions = [w.workflow_session_id for w in active if w.workflow_session_id]
                if sessions:
                    waiting_sessions = set(db.scalars(
                        select(distinct(PipelineJob.workflow_session_id))
                        .where(PipelineJob.status == JobStatus.WAITING_CAPACITY.value,
                               PipelineJob.workflow_session_id.in_(sessions))).all())
                chunks_missing = set(db.scalars(
                    select(distinct(StoryProject.channel_workflow_id))
                    .select_from(AudioGeneration)
                    .join(TTSGeneration, TTSGeneration.id == AudioGeneration.tts_generation_id)
                    .join(StoryVersion, StoryVersion.id == TTSGeneration.story_version_id)
                    .join(StoryProject, StoryProject.id == StoryVersion.story_project_id)
                    .where(AudioGeneration.error_code == "chunks_missing",
                           AudioGeneration.status.in_((DomainStatus.QUEUED.value, DomainStatus.PROCESSING.value)),
                           StoryProject.channel_workflow_id.in_([w.id for w in active]))).all())
            out = []
            for w in wfs:
                states = []
                if w.workflow_session_id in waiting_sessions:
                    states.append(("waiting_capacity", BLOCK_WAITING_CAPACITY))
                if w.id in chunks_missing:
                    states.append(("blocked", BLOCK_CHUNKS_MISSING))
                out.append(WorkflowSummary(
                    id=w.id, name=w.name, mode=w.mode, status=w.status, status_reason=w.status_reason,
                    display_state=_display_from(w.status, w.status_reason, states),
                    project_count=counts.get(w.id, 0), session_id=w.workflow_session_id,
                    created_at=w.created_at, updated_at=w.updated_at, finished_at=w.finished_at))
            return out

    def get_workflow(self, workflow_id: str) -> WorkflowSnapshot:
        now = self.ctx.clock()
        with self.ctx.session_factory() as db:
            wf = db.scalar(select(ChannelWorkflow).where(ChannelWorkflow.id == workflow_id), **_FRESH)
            if wf is None:
                raise NotFound("workflow not found", workflow_id=workflow_id)
            projects = db.scalars(
                select(StoryProject).where(StoryProject.channel_workflow_id == wf.id)
                .order_by(StoryProject.created_at, StoryProject.id), **_FRESH).all()
            snaps = [self._project(db, p, wf, now) for p in projects]
            runners, waiting = [], {}
            if wf.workflow_session_id:
                rows = db.scalars(
                    select(RunnerInstance).where(RunnerInstance.workflow_session_id == wf.workflow_session_id)
                    .order_by(RunnerInstance.runner_type, RunnerInstance.created_at, RunnerInstance.id),
                    **_FRESH).all()
                runners = [_runner_snapshot(r, now) for r in rows]
                waiting = dict(db.execute(
                    select(PipelineJob.role, func.count())
                    .where(PipelineJob.workflow_session_id == wf.workflow_session_id,
                           PipelineJob.status == JobStatus.WAITING_CAPACITY.value)
                    .group_by(PipelineJob.role)).all())
            capacity = _capacity(runners, waiting, has_session=wf.workflow_session_id is not None)
            display = _display_from(wf.status, wf.status_reason,
                                    [(s.state, s.block.kind if s.block else None) for s in snaps])
            tally = {k: sum(1 for s in snaps if s.state == k) for k in
                     ("completed", "failed", "blocked", "in_progress", "waiting_capacity", "not_started")}
            return WorkflowSnapshot(
                id=wf.id, name=wf.name, mode=wf.mode, status=wf.status, status_reason=wf.status_reason,
                status_detail=dict(wf.status_detail) if wf.status_detail else None,
                display_state=display, project_count=len(snaps), counts=WorkflowCounts(**tally),
                session_id=wf.workflow_session_id, created_at=wf.created_at, updated_at=wf.updated_at,
                finished_at=wf.finished_at, projects=snaps, runners=runners, capacity=capacity)

    def get_project(self, project_id: str) -> ProjectSnapshot:
        now = self.ctx.clock()
        with self.ctx.session_factory() as db:
            project = db.scalar(select(StoryProject).where(StoryProject.id == project_id), **_FRESH)
            if project is None:
                raise NotFound("project not found", project_id=project_id)
            wf = None
            if project.channel_workflow_id:
                wf = db.scalar(select(ChannelWorkflow).where(ChannelWorkflow.id == project.channel_workflow_id),
                               **_FRESH)
            return self._project(db, project, wf, now)

    def list_runners(self, *, workflow_id: str | None = None, session_id: str | None = None,
                     unassigned: bool = False) -> list[RunnerSnapshot]:
        if unassigned and (workflow_id or session_id):
            raise ValidationFailed("unassigned cannot be combined with workflow_id/session_id")
        now = self.ctx.clock()
        with self.ctx.session_factory() as db:
            q = select(RunnerInstance).order_by(RunnerInstance.runner_type, RunnerInstance.created_at,
                                                RunnerInstance.id)
            if workflow_id is not None:
                wf = db.scalar(select(ChannelWorkflow).where(ChannelWorkflow.id == workflow_id), **_FRESH)
                if wf is None:
                    raise NotFound("workflow not found", workflow_id=workflow_id)
                if session_id is not None and wf.workflow_session_id != session_id:
                    return []
                session_id = wf.workflow_session_id
                if session_id is None:
                    return []
            if session_id is not None:
                q = q.where(RunnerInstance.workflow_session_id == session_id)
            if unassigned:
                q = q.where(RunnerInstance.workflow_session_id.is_(None))
            return [_runner_snapshot(r, now) for r in db.scalars(q, **_FRESH).all()]

    def get_runner(self, runner_id: str) -> RunnerSnapshot:
        now = self.ctx.clock()
        with self.ctx.session_factory() as db:
            r = db.scalar(select(RunnerInstance).where(RunnerInstance.id == runner_id), **_FRESH)
            if r is None:
                raise NotFound("runner not found", runner_id=runner_id)
            return _runner_snapshot(r, now)

    # ------------------------------------------------------------------ project derivation

    def _find_job(self, db, step: str, domain_id: str | None, job_id: str | None) -> PipelineJob | None:
        if job_id:
            j = db.scalar(select(PipelineJob).where(PipelineJob.id == job_id), **_FRESH)
            if j is not None:
                return j
        prefix = _STEP_DEDUPE_PREFIX.get(step)
        if prefix and domain_id:
            return db.scalar(
                select(PipelineJob).where(PipelineJob.dedupe_key == f"{prefix}:{domain_id}")
                .order_by(PipelineJob.created_at.desc(), PipelineJob.id).limit(1), **_FRESH)
        return None

    def _project(self, db, project: StoryProject, wf: ChannelWorkflow | None, now: datetime) -> ProjectSnapshot:
        steps, jobs, views = [], {}, {}
        current = None
        for handler in self.chain:
            view = handler.status(db, self.ctx, project)
            views[handler.step] = view
            job = self._find_job(db, handler.step, view.domain_id, view.pipeline_job_id) \
                if view.domain_id else None
            jobs[handler.step] = job
            steps.append(StepSummary(step=handler.step, status=view.status.value, domain_id=view.domain_id,
                                     error_code=view.error_code, job=_job_summary(job)))
            if current is None and view.status is not StepStatus.COMPLETED:
                current = handler.step
        cur_view = views.get(current) if current else None

        snap = db.scalars(select(SourceSnapshot).where(
            SourceSnapshot.story_project_id == project.id, SourceSnapshot.status == VersionStatus.ACTIVE.value)
            .order_by(SourceSnapshot.snapshot_number.desc()).limit(1), **_FRESH).first()
        source = None
        if snap is not None:
            meta = snap.meta or {}
            source = SourceSnapshotInfo(
                id=snap.id, snapshot_number=snap.snapshot_number, title=snap.title,
                content_hash=snap.content_hash, language=meta.get("language"),
                language_code=meta.get("language_code"),
                provenance={k: meta[k] for k in ("video_id", "provider", "is_generated", "translated")
                            if k in meta},
                artifact_path=_rel(meta.get("artifact_path")))
        canon = gen = None
        if snap is not None:
            c = _pick(db.scalars(select(CanonAnalysis).where(CanonAnalysis.source_snapshot_id == snap.id),
                                 **_FRESH).all())
            if c is not None:
                canon = CanonInfo(id=c.id, status=c.status, has_canon=bool(c.canon), error_code=c.error_code)
            g = _pick(db.scalars(select(StoryGeneration).where(StoryGeneration.source_snapshot_id == snap.id),
                                 **_FRESH).all())
            if g is not None:
                gen = StoryGenerationInfo(id=g.id, status=g.status, trigger=g.trigger,
                                          error_code=g.error_code, created_at=g.created_at)
        version = db.scalars(select(StoryVersion).where(
            StoryVersion.story_project_id == project.id, StoryVersion.status == VersionStatus.ACTIVE.value)
            .order_by(StoryVersion.version_number.desc()).limit(1), **_FRESH).first()
        story_version = None
        tts = tts_info = audio_info = audio_row = None
        if version is not None:
            story_version = StoryVersionInfo(id=version.id, version_number=version.version_number,
                                             title=version.title, word_count=version.word_count,
                                             content_path=_rel(version.content_path))
            tts = _pick(db.scalars(select(TTSGeneration).where(TTSGeneration.story_version_id == version.id),
                                   **_FRESH).all())
        if tts is not None:
            count = (tts.config or {}).get("chunk_count")
            tts_info = TTSInfo(id=tts.id, status=tts.status, voice=tts.voice, engine=tts.engine,
                               chunk_count=count if isinstance(count, int) else None,
                               error_code=tts.error_code)
            runs = db.scalars(select(AudioGeneration).where(AudioGeneration.tts_generation_id == tts.id)
                              .order_by(AudioGeneration.run_number.desc()), **_FRESH).all()
            audio_row = next((r for r in runs if r.status != DomainStatus.FAILED.value), runs[0] if runs else None)
        registered = 0
        if audio_row is not None:
            chunk_rows = db.scalars(select(AudioChunk).where(
                AudioChunk.audio_generation_id == audio_row.id, AudioChunk.status == VersionStatus.ACTIVE.value)
                .order_by(AudioChunk.chunk_index), **_FRESH).all()
            registered = len(chunk_rows)
            audio_info = AudioInfo(
                id=audio_row.id, run_number=audio_row.run_number, status=audio_row.status,
                chunk_count=audio_row.chunk_count or 0, store_dir=_rel(audio_row.store_dir),
                error_code=audio_row.error_code, registered_chunks=registered,
                chunks=[AudioChunkInfo(c.chunk_index, _rel(c.artifact_path), c.duration_ms or 0)
                        for c in chunk_rows])

        block = self._block(wf, project, current, cur_view, jobs.get(current) if current else None,
                            audio_row, registered, tts_info, now)
        failure = self._failure(wf, project, current, cur_view, jobs.get(current) if current else None)
        state = self._state(current, cur_view, block, failure)
        return ProjectSnapshot(
            id=project.id, workflow_id=project.channel_workflow_id, title=project.title, slug=project.slug,
            status=project.status, state=state, current_step=current,
            step_status=cur_view.status.value if cur_view else None, steps=steps, source=source,
            canon=canon, story_generation=gen, story_version=story_version, tts=tts_info, audio=audio_info,
            block=block, failure=failure)

    @staticmethod
    def _state(current, cur_view, block, failure) -> str:
        if failure is not None:
            return "failed"
        if block is not None and block.kind in _BLOCKING_KINDS:
            return "blocked"
        if block is not None and block.kind == BLOCK_WAITING_CAPACITY:
            return "waiting_capacity"
        if current is None:
            return "completed"
        if cur_view.status is StepStatus.NOT_STARTED and current == "source":
            return "not_started"
        return "in_progress"

    @staticmethod
    def _block(wf, project, current, cur_view, job, audio_row, registered, tts_info, now) -> BlockInfo | None:
        # Defensive: FINISHED must mean every step completed.
        if wf is not None and wf.status == ChannelWorkflowStatus.FINISHED.value and current is not None:
            return BlockInfo(BLOCK_INCONSISTENT, f"workflow is finished but step '{current}' is not completed")
        # Phase 4: audio job succeeded but chunks are missing -> never "complete".
        if audio_row is not None:
            missing_flag = audio_row.error_code == "chunks_missing" and \
                audio_row.status in (DomainStatus.QUEUED.value, DomainStatus.PROCESSING.value)
            expected = audio_row.chunk_count or (tts_info.chunk_count if tts_info and tts_info.chunk_count else 0)
            short = audio_row.status == DomainStatus.COMPLETED.value and expected and registered < expected
            if missing_flag or short:
                return BlockInfo(BLOCK_CHUNKS_MISSING, _bound(audio_row.error_message)
                                 or f"{registered} of {expected} audio chunks registered")
        if current is None:
            return None
        if wf is not None and wf.status == ChannelWorkflowStatus.PAUSED.value and \
                wf.status_reason == PauseReason.STEP_FAILED.value:
            detail = wf.status_detail or {}
            if detail.get("project_id") == project.id and detail.get("error_code") == "provider_blocked":
                return BlockInfo(BLOCK_PROVIDER_BLOCKED, "provider blocked the request")
        if cur_view.error_code == "provider_blocked":
            return BlockInfo(BLOCK_PROVIDER_BLOCKED, "provider blocked the request")
        if job is not None and job.status == JobStatus.WAITING_CAPACITY.value:
            return BlockInfo(BLOCK_WAITING_CAPACITY, _bound(job.last_error_message)
                             or f"no eligible runner for role {job.role}")
        if job is not None and job.status == JobStatus.QUEUED.value and job.scheduled_at and job.scheduled_at > now:
            return BlockInfo(BLOCK_DELAYED, "job scheduled in the future", until=job.scheduled_at)
        return None

    @staticmethod
    def _failure(wf, project, current, cur_view, job) -> FailureInfo | None:
        if current is None:
            return None
        job_failed = job is not None and job.status in (JobStatus.FAILED.value, JobStatus.CANCELLED.value)
        if cur_view.status is StepStatus.FAILED or job_failed:
            code = cur_view.error_code or (job.last_error_code if job else None)
            return FailureInfo(
                category=categorize_error(code), code=code,
                message=_bound(job.last_error_message) if job else None, step=current,
                attempts=job.attempts if job else None, max_attempts=job.max_attempts if job else None,
                infrastructure_failures=job.infrastructure_failures if job else None,
                max_infra_attempts=job.max_infra_attempts if job else None)
        # Non-durable failures (e.g. inline source step) survive only in the workflow pause detail.
        if wf is not None and wf.status == ChannelWorkflowStatus.PAUSED.value and \
                wf.status_reason == PauseReason.STEP_FAILED.value:
            detail = wf.status_detail or {}
            if detail.get("project_id") == project.id and detail.get("step") == current:
                code = detail.get("error_code")
                return FailureInfo(category=categorize_error(code), code=code, message=None, step=current,
                                   attempts=None, max_attempts=None, infrastructure_failures=None,
                                   max_infra_attempts=None)
        return None
