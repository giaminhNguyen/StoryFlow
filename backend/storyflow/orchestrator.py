"""Phase 4 workflow orchestrator: a restartable state machine over durable DB state.

Nothing about progress lives in memory. Every tick re-derives, per project, the first step of
the chain (source -> canon -> story -> tts -> audio) that is not COMPLETED by asking the
handlers (``handler.status``) and then does at most the one thing that step needs:

  NOT_STARTED  inline step -> ``source.run``; job step -> begin -> enqueue_job -> link_job
  IN_PROGRESS  no linked job (crash between begin and enqueue) -> repair by enqueueing;
               job COMPLETED -> finalize (idempotent) and keep walking the chain;
               job FAILED/CANCELLED -> mark_failed, workflow PAUSED;
               job queued/processing/waiting_capacity -> wait (waiting_capacity is not a failure)
  FAILED       stop the project, workflow PAUSED; only an explicit ``retry_failed_step`` /
               ``resume`` creates a fresh domain row.

The orchestrator never talks to a runner: execution only happens inside
``Dispatcher.run_round``. It only *observes* PipelineJob.status, so Phase 2 retry / infra /
business / quota / waiting semantics are untouched. Duplicate work is prevented by the
enqueue dedupe_key partial unique index plus each handler's unique-index-guarded ``begin``,
so ticking N times or from many threads/processes yields exactly one active job per step.
No DB transaction is held across ``source.run`` or the dispatcher.
"""

import contextlib
import logging
import threading
from dataclasses import dataclass, field

from sqlalchemy import func, select, update
from sqlalchemy.exc import OperationalError

from . import queue
from .dispatcher import Dispatcher, DispatchOutcome
from .models import (
    ChannelWorkflow,
    ChannelWorkflowStatus,
    JobStatus,
    PauseReason,
    PipelineJob,
    ProjectStatus,
    SourceSnapshot,
    StoryProject,
    TERMINAL_PROJECT_STATUSES,
    VersionStatus,
    WorkflowSession,
)
from .pipeline import InlineStepHandler, PipelineContext, StepHandler, StepStatus
from .policy import BREAKER_LIMIT, PERMANENT, batch_from_config, is_systemic_error, policy_from_config, terminal_status_for

logger = logging.getLogger(__name__)

_FRESH = {"execution_options": {"populate_existing": True}}
_MAX_STEP_PASSES = 4  # begin -> link -> finalize -> re-status; bounded so a buggy handler can't spin

# Step outcomes (internal). _ENDED: the project reached a terminal batch outcome (skipped /
# needs_attention) under the workflow's failure policy; it is not a workflow failure.
_DONE, _WAIT, _FAILED, _AGAIN, _ENDED = "done", "wait", "failed", "again", "ended"


@dataclass
class ProjectState:
    """Where a project currently is: the first non-completed step (None = all completed)."""

    step: str | None
    status: StepStatus | None
    domain_id: str | None = None
    job_id: str | None = None
    error_code: str | None = None
    terminal: str | None = None    # "skipped" | "needs_attention": the project is done for this batch


@dataclass
class TickResult:
    workflow_status: str
    projects: dict[str, ProjectState] = field(default_factory=dict)
    jobs_enqueued: list[str] = field(default_factory=list)   # job ids scheduled/linked (idempotent)
    finalized: list[tuple[str, str]] = field(default_factory=list)   # (project_id, step)
    failed: list[tuple[str, str, str | None]] = field(default_factory=list)  # (project_id, step, error)
    ran_inline: list[tuple[str, str]] = field(default_factory=list)
    ended: list[tuple[str, str, str | None]] = field(default_factory=list)   # (project_id, step, error_code)
    recovered: int = 0

    @property
    def current_steps(self) -> dict[str, str | None]:
        return {pid: st.step for pid, st in self.projects.items()}


@dataclass
class RoundResult:
    workflow_status: str
    outcome: DispatchOutcome | None      # None when the workflow was not ACTIVE (no dispatch)
    dispatched_job_id: str | None
    progressed: bool                     # any durable state changed during this round
    tick_before: TickResult
    tick_after: TickResult

    @property
    def jobs_enqueued(self) -> list[str]:
        return self.tick_before.jobs_enqueued + self.tick_after.jobs_enqueued

    @property
    def projects(self) -> dict[str, ProjectState]:
        return self.tick_after.projects


@dataclass
class RunResult:
    status: str                          # completed | paused | blocked | max_rounds
    workflow_status: str
    rounds: int
    outcomes: list[DispatchOutcome] = field(default_factory=list)
    jobs_enqueued: list[str] = field(default_factory=list)
    projects: dict[str, ProjectState] = field(default_factory=dict)
    blocked_reason: str | None = None    # waiting_capacity | no_progress | None

    @property
    def current_steps(self) -> dict[str, str | None]:
        return {pid: st.step for pid, st in self.projects.items()}


class Orchestrator:
    def __init__(self, ctx: PipelineContext, dispatcher: Dispatcher, source: InlineStepHandler,
                 steps: list[StepHandler]):
        self.ctx = ctx
        self.dispatcher = dispatcher
        self.source = source
        self.steps = list(steps)
        self.chain: list = [source, *self.steps]
        self._fingerprints: dict[str, tuple] = {}   # workflow id -> state after its last round (see run_round)
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------ helpers

    def _lock_for(self, workflow_id: str | None):
        """In-process lock per workflow: a tick and an operator command (retry / resume / reactivate) never
        interleave, so a tick cannot observe a half-armed project. Never held while a job is dispatched. Other
        processes are still covered by the compare-and-set on the workflow status and the unique indexes."""
        if workflow_id is None:
            return contextlib.nullcontext()
        with self._locks_guard:
            return self._locks.setdefault(workflow_id, threading.RLock())

    def _workflow_id_of(self, project_id: str) -> str | None:
        with self.ctx.session_factory() as db:
            return db.scalar(select(StoryProject.channel_workflow_id).where(StoryProject.id == project_id), **_FRESH)

    def _now(self, now):
        return now or self.ctx.clock()

    def _workflow(self, db, workflow_id: str) -> ChannelWorkflow:
        wf = db.scalar(select(ChannelWorkflow).where(ChannelWorkflow.id == workflow_id), **_FRESH)
        if wf is None:
            raise LookupError(f"workflow {workflow_id} not found")
        return wf

    def _project_ids(self, db, workflow_id: str) -> list[str]:
        return list(db.scalars(
            select(StoryProject.id).where(StoryProject.channel_workflow_id == workflow_id)
            .order_by(StoryProject.created_at, StoryProject.id), **_FRESH,
        ).all())

    def _set_workflow_status(self, db, workflow_id: str, new: ChannelWorkflowStatus, *, expect: ChannelWorkflowStatus,
                             now, reason: PauseReason | None = None, detail: dict | None = None) -> bool:
        """Guarded transition (compare-and-set on status); returns True if this call won it.
        status_reason/status_detail are only meaningful while PAUSED and are cleared otherwise."""
        values = {"status": new.value, "updated_at": now,
                  "status_reason": reason.value if reason else None, "status_detail": detail}
        if new is ChannelWorkflowStatus.FINISHED:
            values["finished_at"] = now
        elif new is ChannelWorkflowStatus.ACTIVE:
            values["finished_at"] = None     # re-opened (a skipped / failed project was retried)
        res = db.execute(update(ChannelWorkflow)
                         .where(ChannelWorkflow.id == workflow_id, ChannelWorkflow.status == expect.value)
                         .values(**values))
        db.commit()
        won = res.rowcount == 1
        if won:
            logger.info("workflow_transition workflow=%s from=%s to=%s reason=%s%s", workflow_id, expect.value,
                        new.value, reason.value if reason else "-",
                        "".join(f" {k}={str(v)[:80]}" for k, v in (detail or {}).items()
                                if k in ("project_id", "step", "error_code")))
        return won

    def _job(self, db, job_id: str | None) -> PipelineJob | None:
        if not job_id:
            return None
        return db.scalar(select(PipelineJob).where(PipelineJob.id == job_id), **_FRESH)

    def _schedule(self, db, handler: StepHandler, project_id: str, wf: ChannelWorkflow, now, res: TickResult,
                  *, require_active: bool = True):
        """begin -> enqueue (deduped) -> link. Safe to repeat and to race.
        Returns (domain_id, job) or None when a prerequisite is missing."""
        db.expire_all()
        # Narrow the cancel/pause race: never create new work for a workflow that stopped being
        # ACTIVE since this tick started (a pause/cancel command is a compare-and-set on status).
        if require_active and self._workflow(db, wf.id).status != ChannelWorkflowStatus.ACTIVE.value:
            return None
        project = db.get(StoryProject, project_id)
        begun = handler.begin(db, self.ctx, project)
        if begun is None:
            return None
        domain_id, spec = begun
        # The job for this domain row may already exist (crash between enqueue and link, or a
        # handler without a link column): reuse the latest job of ANY status. A terminal job frees
        # the dedupe slot, so enqueueing again would silently grant extra retries / duplicates.
        # dedupe_key is derived from the domain row id, so a retry (fresh row) gets a fresh key.
        job = db.scalar(
            select(PipelineJob)
            .where(PipelineJob.dedupe_key == spec.dedupe_key)
            .order_by(PipelineJob.created_at.desc()).limit(1), **_FRESH,
        )
        if job is None:
            job = queue.enqueue_job(
                db, kind=spec.kind, payload=spec.payload, dedupe_key=spec.dedupe_key,
                priority=spec.priority, session_id=wf.workflow_session_id, role=spec.role, now=now,
            )
        handler.link_job(db, self.ctx, domain_id, job)
        res.jobs_enqueued.append(job.id)
        return domain_id, job

    # ------------------------------------------------------------------ chain walk

    def _step(self, db, handler, project_id: str, wf: ChannelWorkflow, now, res: TickResult) -> str:
        db.expire_all()
        project = db.get(StoryProject, project_id)
        view = handler.status(db, self.ctx, project)
        if view.status is StepStatus.COMPLETED:
            return _DONE
        if view.status is StepStatus.FAILED:
            if self._end_by_policy(db, project_id, wf, handler.step, view.error_code):
                res.ended.append((project_id, handler.step, view.error_code))
                return _ENDED
            return _FAILED
        inline = isinstance(handler, InlineStepHandler)
        if view.status is StepStatus.NOT_STARTED:
            if inline:
                db.commit()  # no transaction across external I/O
                out = handler.run(self.ctx, project_id)
                res.ran_inline.append((project_id, handler.step))
                if out.status is StepStatus.COMPLETED:
                    return _DONE
                if out.status is StepStatus.FAILED:
                    if self._is_terminal(db, project_id):  # policy: skip / continue, never pause the batch
                        logger.info("project_ended project=%s step=%s error_code=%s", project_id, handler.step,
                                    str(out.error_code)[:80])
                        res.ended.append((project_id, handler.step, out.error_code))
                        return _ENDED
                    res.failed.append((project_id, handler.step, out.error_code))
                    return _FAILED
                return _WAIT
            scheduled = self._schedule(db, handler, project_id, wf, now, res)
            if scheduled is None:
                return _WAIT
            domain_id, job = scheduled
            return self._reconcile(db, handler, project_id, domain_id, job, res, wf)
        # IN_PROGRESS
        if inline:
            return _WAIT
        domain_id = view.domain_id
        job = self._job(db, view.pipeline_job_id)
        if job is None:  # begin committed but enqueue/link never did: repair
            scheduled = self._schedule(db, handler, project_id, wf, now, res)
            if scheduled is None:
                return _WAIT
            domain_id, job = scheduled
        return self._reconcile(db, handler, project_id, domain_id, job, res, wf)

    def _reconcile(self, db, handler, project_id: str, domain_id: str, job: PipelineJob, res: TickResult,
                   wf: ChannelWorkflow) -> str:
        """Apply what the (already linked/found) job's status means for its step."""
        if job.status == JobStatus.COMPLETED.value:
            handler.finalize(db, self.ctx, domain_id, job)
            logger.info("step_finalized project=%s step=%s job=%s", project_id, handler.step, job.id)
            res.finalized.append((project_id, handler.step))
            return _AGAIN
        if job.status in (JobStatus.FAILED.value, JobStatus.CANCELLED.value):
            handler.mark_failed(db, self.ctx, domain_id, job)
            logger.info("step_failed project=%s step=%s job=%s error_code=%s", project_id, handler.step, job.id,
                        str(job.last_error_code)[:80])
            # a step may tolerate its own failure (an advisory review with ``on_failure: skip``): if it now reports
            # COMPLETED the chain simply moves on: not a workflow failure and not a reason to end the project
            db.expire_all()
            if handler.status(db, self.ctx, db.get(StoryProject, project_id)).status is StepStatus.COMPLETED:
                return _AGAIN
            code = job.last_error_code or ("cancelled" if job.status == JobStatus.CANCELLED.value else None)
            if self._end_by_policy(db, project_id, wf, handler.step, code):
                res.ended.append((project_id, handler.step, job.last_error_code))
                return _ENDED
            res.failed.append((project_id, handler.step, job.last_error_code))
            return _FAILED
        return _WAIT  # queued / processing / waiting_capacity

    def _end_by_policy(self, db, project_id: str, wf: ChannelWorkflow, step: str, code: str | None) -> bool:
        """``failure_policy.on_permanent_error == "continue"``: a step that failed for good ends only THIS project
        (needs_attention) instead of pausing the whole workflow. Returns True when the project was ended.

        Only an error of THAT item qualifies: a systemic code (quota / auth / infrastructure / capacity, see
        ``policy.SYSTEMIC_CODES``) would hit every project, so the workflow pauses instead. A circuit breaker also
        pauses it once ``BREAKER_LIMIT`` projects already ended with the same reason: the cause is not the item."""
        status = terminal_status_for(policy_from_config(wf.config), PERMANENT)
        if status is None or is_systemic_error(code):
            return False
        db.expire_all()
        project = db.get(StoryProject, project_id)
        if project is None or project.status in TERMINAL_PROJECT_STATUSES:
            return project is not None
        reason = str(code or "step_failed")[:64]
        same = db.scalar(select(func.count()).select_from(StoryProject).where(
            StoryProject.channel_workflow_id == wf.id, StoryProject.status == ProjectStatus.NEEDS_ATTENTION.value,
            StoryProject.status_reason == reason)) or 0
        if same >= BREAKER_LIMIT:
            logger.warning("circuit_breaker workflow=%s reason=%s already_ended=%d: pausing instead of ending "
                           "project=%s", wf.id, reason, same, project_id)
            return False
        project.status = status
        project.status_reason = reason
        project.status_detail = {"step": step, "error_code": code}
        project.next_attempt_at = None
        project.updated_at = self.ctx.clock()
        db.commit()
        logger.info("project_ended project=%s step=%s status=%s error_code=%s", project_id, step, status,
                    str(code)[:80])
        return True

    @staticmethod
    def _is_terminal(db, project_id: str) -> bool:
        db.expire_all()
        project = db.get(StoryProject, project_id)
        return project is not None and project.status in TERMINAL_PROJECT_STATUSES

    def _prefetch_source(self, db, project_id: str, wf: ChannelWorkflow, now, res: TickResult) -> None:
        """Run ONLY the inline source step of a project that is waiting for a batch slot.

        Fetching the transcript is the slow, provider-bound part of a batch and it creates NO job, so it
        does not compete for ``batch.max_active``: every waiting project downloads its subtitles while the
        window is still busy, and canon / story / review / tts / audio (the expensive, job-backed steps) keep
        waiting for their slot exactly as before. The caller's slot accounting is untouched: a prefetched
        project is still queued, in creation order, for a slot on a later tick.

        Failures keep the source step's own semantics (transient -> durable backoff, ``skip`` / ``continue`` ->
        only this project ends, otherwise the workflow pauses), because this is the very same ``_step`` call
        the in-window path makes.

        Scope of the decoupling: this only takes the source step OUT of the batch window. It is NOT a
        wall-clock concurrency guarantee against a synchronous runner - every prefetch happens in sequence
        inside one tick, before that tick dispatches, so a very large batch pays the provider latency once
        up front and the round that pays it dispatches later. Bound it per tick (a cap) if that ever matters.
        """
        self._step(db, self.source, project_id, wf, now, res)

    def _handlers(self, db, project_id: str) -> list:
        """The chain of THIS project: steps that are switched off for its workflow (``enabled`` False, e.g.
        ``review`` under the fast preset) do not exist for it."""
        project = db.get(StoryProject, project_id)
        return [h for h in self.chain if getattr(h, "enabled", None) is None or h.enabled(db, self.ctx, project)]

    def _advance_project(self, db, project_id: str, wf: ChannelWorkflow, now, res: TickResult) -> None:
        db.expire_all()
        project = db.get(StoryProject, project_id)
        if project is None or project.status in TERMINAL_PROJECT_STATUSES \
                or project.status == ProjectStatus.COMPLETED.value:
            return  # skipped / needs_attention / completed: nothing more to do for this project
        for handler in self._handlers(db, project_id):
            outcome = _AGAIN
            for _ in range(_MAX_STEP_PASSES):
                outcome = self._step(db, handler, project_id, wf, now, res)
                if outcome != _AGAIN:
                    break
            if outcome == _DONE:
                continue
            return  # waiting, failed, or (bounded) unresolved: nothing later may advance

    def _position(self, db, project_id: str) -> ProjectState:
        """Where the project is. The session is refreshed ONCE (nothing here writes, so nothing changes between
        the step reads); a finished project is recorded as ``completed`` so later calls are a single row read."""
        db.expire_all()
        project = db.get(StoryProject, project_id)
        if project is not None:
            if project.status in TERMINAL_PROJECT_STATUSES:
                return ProjectState(None, None, error_code=project.status_reason, terminal=project.status)
            if project.status == ProjectStatus.COMPLETED.value:
                return ProjectState(None, None)
        for handler in self._handlers(db, project_id):
            view = handler.status(db, self.ctx, project)
            if view.status is not StepStatus.COMPLETED:
                return ProjectState(handler.step, view.status, view.domain_id, view.pipeline_job_id, view.error_code)
        if project is not None and project.status == ProjectStatus.ACTIVE.value:
            self._mark_completed(db, project_id)
        return ProjectState(None, None)

    def _mark_completed(self, db, project_id: str) -> None:
        db.execute(update(StoryProject)
                   .where(StoryProject.id == project_id, StoryProject.status == ProjectStatus.ACTIVE.value)
                   .values(status=ProjectStatus.COMPLETED.value, updated_at=self.ctx.clock())
                   .execution_options(synchronize_session=False))
        db.commit()

    # ------------------------------------------------------------------ public API

    def tick(self, workflow_id: str, *, now=None) -> TickResult:
        """recover stale jobs + reconcile in-flight steps + schedule the next missing step.
        Never dispatches. A non-ACTIVE workflow does nothing."""
        with self._lock_for(workflow_id):
            return self._tick(workflow_id, now=now)

    def _safe_position(self, db, project_id: str) -> ProjectState:
        """``_position`` that cannot take a whole tick down: a project whose state cannot even be read is reported
        as a failed 'orchestrator' step (the workflow pauses visibly) instead of raising every iteration."""
        try:
            return self._position(db, project_id)
        except OperationalError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("project_position_failed project=%s", project_id)
            db.rollback()
            return ProjectState("orchestrator", StepStatus.FAILED, error_code="internal_error")

    def _contain(self, db, wf: ChannelWorkflow, project_id: str, res: TickResult) -> None:
        """One project raised while it was advanced: log it and handle it like any permanent item error (ended under
        ``continue``, else the workflow pauses with a visible step_failed) so the others are not blocked."""
        logger.exception("project_advance_failed workflow=%s project=%s", wf.id, project_id)
        db.rollback()
        if self._end_by_policy(db, project_id, wf, "orchestrator", "internal_error"):
            res.ended.append((project_id, "orchestrator", "internal_error"))
        else:
            res.failed.append((project_id, "orchestrator", "internal_error"))

    def _tick(self, workflow_id: str, *, now=None) -> TickResult:
        now = self._now(now)
        db = self.ctx.session_factory()
        try:
            wf = self._workflow(db, workflow_id)
            res = TickResult(workflow_status=wf.status)
            if wf.status != ChannelWorkflowStatus.ACTIVE.value:
                res.projects = {pid: self._position(db, pid) for pid in self._project_ids(db, workflow_id)}
                return res
            res.recovered = queue.recover_stale_jobs(db, now=now)
            pids = self._project_ids(db, workflow_id)
            slots = batch_from_config(wf.config).max_active      # None = every project at once (legacy)
            seen: dict[str, ProjectState] = {}    # positions that cannot have changed since they were read
            for pid in pids:
                in_window = True
                if slots is not None:
                    pos = self._position(db, pid)
                    if pos.step is None:
                        seen[pid] = pos
                        continue                                  # completed / skipped: takes no slot
                    if slots <= 0:
                        # no free slot: the heavy steps wait, but the project may still fetch its subtitles
                        # (inline, no job -> nothing competes for capacity). See ``_prefetch_source``.
                        in_window = False
                        seen[pid] = pos
                        if pos.step != self.source.step:
                            continue
                    else:
                        slots -= 1
                try:
                    if in_window:
                        self._advance_project(db, pid, wf, now, res)
                    else:
                        self._prefetch_source(db, pid, wf, now, res)
                except OperationalError:
                    raise                   # database busy / locked: transient, the next iteration simply retries
                except Exception:  # noqa: BLE001 - one bad project must not stop the round for the others
                    self._contain(db, wf, pid, res)
                if slots is not None:
                    after = seen[pid] = self._safe_position(db, pid)
                    if in_window and after.step is None:
                        slots += 1          # finished / ended during this very tick: the next project may start now
            res.projects = {pid: seen[pid] if pid in seen else self._safe_position(db, pid) for pid in pids}
            failure = self._first_failure(res)
            if failure is not None:
                project_id, step, error_code = failure
                self._set_workflow_status(
                    db, workflow_id, ChannelWorkflowStatus.PAUSED, expect=ChannelWorkflowStatus.ACTIVE, now=now,
                    reason=PauseReason.STEP_FAILED,
                    detail={"project_id": project_id, "step": step, "error_code": error_code})
            elif pids and all(st.step is None for st in res.projects.values()) \
                    and set(self._project_ids(db, workflow_id)) == set(pids):
                # a project added meanwhile (add_sources / add_project) is not in ``pids``: do not finish over it
                if self._set_workflow_status(db, workflow_id, ChannelWorkflowStatus.FINISHED,
                                             expect=ChannelWorkflowStatus.ACTIVE, now=now):
                    self._reopen_if_stranded(db, workflow_id, now)
            res.workflow_status = self._workflow(db, workflow_id).status
            return res
        finally:
            db.close()

    def _reopen_if_stranded(self, db, workflow_id: str, now) -> None:
        """After the FINISHED compare-and-set won: a project may have been added / re-armed between the position
        scan and the CAS (the writer saw ACTIVE and therefore did not re-open). Look again; if anything is not
        closed, FINISHED -> ACTIVE so it is not stranded in a finished workflow."""
        if any(self._safe_position(db, pid).step is not None for pid in self._project_ids(db, workflow_id)):
            self._set_workflow_status(db, workflow_id, ChannelWorkflowStatus.ACTIVE,
                                      expect=ChannelWorkflowStatus.FINISHED, now=now)

    @staticmethod
    def _first_failure(res: TickResult):
        """(project_id, step, error_code) of the first failure seen this tick, else of the first
        project whose current step is FAILED; None when nothing failed."""
        if res.failed:
            return res.failed[0]
        for pid, st in res.projects.items():
            if st.status is StepStatus.FAILED:
                return pid, st.step, st.error_code
        return None

    def _fingerprint(self, workflow_id: str, res_projects: dict[str, ProjectState] | None = None):
        db = self.ctx.session_factory()
        try:
            wf = self._workflow(db, workflow_id)
            projects = res_projects
            if projects is None:
                projects = {pid: self._position(db, pid) for pid in self._project_ids(db, workflow_id)}
            jobs = ()
            if wf.workflow_session_id is not None:
                rows = db.execute(
                    select(PipelineJob.id, PipelineJob.status, PipelineJob.attempts,
                           PipelineJob.infrastructure_failures, PipelineJob.execution_count,
                           PipelineJob.scheduled_at)
                    .where(PipelineJob.workflow_session_id == wf.workflow_session_id)
                    .order_by(PipelineJob.id), **_FRESH,
                ).all()
                jobs = tuple(tuple(r) for r in rows)
            pos = tuple(sorted((pid, st.step, st.status, st.domain_id, st.job_id) for pid, st in projects.items()))
            return (wf.status, pos, jobs)
        finally:
            db.close()

    def run_round(self, workflow_id: str, *, now=None) -> RoundResult:
        """tick -> one Dispatcher.run_round for the workflow's session -> tick again so a job
        that just finished advances the chain immediately."""
        now = self._now(now)
        # the previous round's end state is this round's start state (one fewer full scan per round); a change
        # made in between only makes ``progressed`` True, which is always safe
        start = self._fingerprints.get(workflow_id)
        if start is None:
            start = self._fingerprint(workflow_id)
        before = self.tick(workflow_id, now=now)
        if before.workflow_status != ChannelWorkflowStatus.ACTIVE.value:
            end = self._fingerprints[workflow_id] = self._fingerprint(workflow_id, before.projects)
            return RoundResult(before.workflow_status, None, None, end != start, before, before)
        db = self.ctx.session_factory()
        try:
            wf = self._workflow(db, workflow_id)
            if wf.workflow_session_id is None:
                raise ValueError(f"workflow {workflow_id} has no workflow_session_id; cannot dispatch")
            session = db.get(WorkflowSession, wf.workflow_session_id)
            db.commit()  # release the read snapshot; dispatcher opens its own short transactions
            outcome, job_id = self.dispatcher.run_round(db, session, now=now)
        finally:
            db.close()
        # nothing was dispatched -> nothing can have finished: the first tick's view is still the truth
        after = before if job_id is None else self.tick(workflow_id, now=now)
        end = self._fingerprints[workflow_id] = self._fingerprint(workflow_id, after.projects)
        return RoundResult(after.workflow_status, outcome, job_id, end != start, before, after)

    def run_until_idle(self, workflow_id: str, *, max_rounds: int = 200, now=None) -> RunResult:
        """Loop run_round until completed / paused / blocked / max_rounds. Never sleeps and never
        spins: a round with no durable state change (nothing dispatchable, job parked in
        waiting_capacity, queued for the future) returns 'blocked'."""
        out = RunResult(status="max_rounds", workflow_status="", rounds=0)
        for _ in range(max_rounds):
            rnd = self.run_round(workflow_id, now=now)
            out.rounds += 1
            out.workflow_status = rnd.workflow_status
            out.projects = rnd.projects
            out.jobs_enqueued += rnd.jobs_enqueued
            if rnd.outcome is not None:
                out.outcomes.append(rnd.outcome)
            if rnd.workflow_status == ChannelWorkflowStatus.FINISHED.value:
                out.status = "completed"
                return out
            if rnd.workflow_status != ChannelWorkflowStatus.ACTIVE.value:
                out.status = "paused" if rnd.workflow_status == ChannelWorkflowStatus.PAUSED.value else "blocked"
                return out
            if rnd.outcome is DispatchOutcome.PARKED_NO_CANDIDATE:
                out.status, out.blocked_reason = "blocked", "waiting_capacity"
                return out
            if not rnd.progressed:
                out.status, out.blocked_reason = "blocked", "no_progress"
                return out
        return out

    # ------------------------------------------------------------------ explicit operator actions

    def _retry_project(self, db, project_id: str, wf: ChannelWorkflow, now) -> str | None:
        """Re-arm the first non-completed step if it is FAILED. Returns the step name or None.
        An ended (skipped / needs_attention) project is left alone: only ``reactivate_project`` may bring it back,
        otherwise resume / retry would queue jobs for a project the batch already gave up on."""
        if self._is_terminal(db, project_id):
            return None
        self._reset_source_retry(db, project_id)
        for handler in self._handlers(db, project_id):
            db.expire_all()
            view = handler.status(db, self.ctx, db.get(StoryProject, project_id))
            if view.status is StepStatus.COMPLETED:
                continue
            if view.status is not StepStatus.FAILED:
                return None
            if isinstance(handler, InlineStepHandler):
                db.commit()
                handler.run(self.ctx, project_id)
            else:
                self._schedule(db, handler, project_id, wf, now, TickResult(workflow_status=wf.status),
                               require_active=False)
            return handler.step
        return None

    @staticmethod
    def _reset_source_retry(db, project_id: str) -> None:
        """An operator retry / resume gives a project whose subtitles are still missing a fresh set of source
        attempts (otherwise ``subtitle_retries_exhausted`` would come back after a single try). Only the
        source-retry state is touched, and only while there is no snapshot yet."""
        db.expire_all()
        project = db.get(StoryProject, project_id)
        if project is None or project.status != ProjectStatus.ACTIVE.value:
            return
        if not (project.source_attempts or project.next_attempt_at):
            return
        has_snapshot = db.scalar(select(SourceSnapshot.id).where(
            SourceSnapshot.story_project_id == project_id, SourceSnapshot.status == VersionStatus.ACTIVE.value)
            .limit(1)) is not None
        if has_snapshot:
            return
        project.source_attempts = 0
        project.next_attempt_at = None
        detail = {k: v for k, v in (project.status_detail or {}).items()
                  if k not in ("last_error", "attempts", "retry_in_seconds")}
        project.status_detail = detail or None
        db.commit()

    def reactivate_project(self, project_id: str, *, now=None) -> str | None:
        """Operator action (serialised with the workflow's ticks): see ``_reactivate_project``."""
        with self._lock_for(self._workflow_id_of(project_id)):
            return self._reactivate_project(project_id, now=now)

    def _reactivate_project(self, project_id: str, *, now=None) -> str | None:
        """Operator action: bring a skipped / needs_attention project back into its workflow. Returns the step
        that had ended it (None when the project was not ended). A job step that failed gets a fresh domain row
        + job; the inline source step simply runs again on the next tick. A finished workflow is re-opened."""
        now = self._now(now)
        db = self.ctx.session_factory()
        try:
            project = db.get(StoryProject, project_id)
            if project is None or project.status not in TERMINAL_PROJECT_STATUSES:
                return None
            if project.channel_workflow_id is None:
                return None
            wf = self._workflow(db, project.channel_workflow_id)
            if wf.status in (ChannelWorkflowStatus.CANCELLED.value, ChannelWorkflowStatus.ABANDONED.value):
                return None   # history of a cancelled workflow is kept as it is; nothing may be queued for it
            step = (project.status_detail or {}).get("step")
            project.status = ProjectStatus.ACTIVE.value
            project.status_reason = None
            project.status_detail = None
            project.source_attempts = 0
            project.next_attempt_at = None
            project.updated_at = now
            db.commit()
            wf = self._workflow(db, wf.id)
            self._retry_project(db, project_id, wf, now)
            if wf.status == ChannelWorkflowStatus.FINISHED.value:
                self._set_workflow_status(db, wf.id, ChannelWorkflowStatus.ACTIVE,
                                          expect=ChannelWorkflowStatus.FINISHED, now=now)
            return step
        finally:
            db.close()

    def failed_step_of(self, project_id: str) -> str | None:
        """Read-only: the name of the project's current step if (and only if) it is FAILED. Inline-step
        failures (source) leave no failed domain row, so they are NOT visible here (see status_detail)."""
        db = self.ctx.session_factory()
        try:
            pos = self._position(db, project_id)
            return pos.step if pos.status is StepStatus.FAILED else None
        finally:
            db.close()

    def retry_failed_step(self, project_id: str, *, now=None) -> str | None:
        """Operator action (serialised with the workflow's ticks): see ``_retry_failed_step``."""
        with self._lock_for(self._workflow_id_of(project_id)):
            return self._retry_failed_step(project_id, now=now)

    def _retry_failed_step(self, project_id: str, *, now=None) -> str | None:
        """Operator action: give a project's FAILED step a fresh domain row + job, and reactivate
        the workflow if that was its last failure. Never called automatically."""
        now = self._now(now)
        db = self.ctx.session_factory()
        try:
            project = db.get(StoryProject, project_id)
            wf = self._workflow(db, project.channel_workflow_id)
            step = self._retry_project(db, project_id, wf, now)
            if wf.status == ChannelWorkflowStatus.PAUSED.value:
                still_failed = any(
                    self._position(db, pid).status is StepStatus.FAILED
                    for pid in self._project_ids(db, wf.id)
                )
                if not still_failed:
                    self._set_workflow_status(db, wf.id, ChannelWorkflowStatus.ACTIVE,
                                              expect=ChannelWorkflowStatus.PAUSED, now=now)
            return step
        finally:
            db.close()

    def resume(self, workflow_id: str, *, now=None) -> str:
        """Operator action (serialised with the workflow's ticks): see ``_resume``."""
        with self._lock_for(workflow_id):
            return self._resume(workflow_id, now=now)

    def _resume(self, workflow_id: str, *, now=None) -> str:
        """Operator action: retry every FAILED step of a PAUSED workflow, then PAUSED -> ACTIVE.
        FINISHED / ABANDONED workflows are left untouched. Returns the workflow status."""
        now = self._now(now)
        db = self.ctx.session_factory()
        try:
            wf = self._workflow(db, workflow_id)
            if wf.status != ChannelWorkflowStatus.PAUSED.value:
                return wf.status
            for pid in self._project_ids(db, workflow_id):
                self._retry_project(db, pid, wf, now)
            self._set_workflow_status(db, workflow_id, ChannelWorkflowStatus.ACTIVE,
                                      expect=ChannelWorkflowStatus.PAUSED, now=now)
            return self._workflow(db, workflow_id).status
        finally:
            db.close()
