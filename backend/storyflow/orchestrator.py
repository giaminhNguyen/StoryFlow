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

from dataclasses import dataclass, field

from sqlalchemy import select, update

from . import queue
from .dispatcher import Dispatcher, DispatchOutcome
from .models import (
    ChannelWorkflow,
    ChannelWorkflowStatus,
    JobStatus,
    PauseReason,
    PipelineJob,
    StoryProject,
    WorkflowSession,
)
from .pipeline import InlineStepHandler, PipelineContext, StepHandler, StepStatus

_FRESH = {"execution_options": {"populate_existing": True}}
_MAX_STEP_PASSES = 4  # begin -> link -> finalize -> re-status; bounded so a buggy handler can't spin

# Step outcomes (internal)
_DONE, _WAIT, _FAILED, _AGAIN = "done", "wait", "failed", "again"


@dataclass
class ProjectState:
    """Where a project currently is: the first non-completed step (None = all completed)."""

    step: str | None
    status: StepStatus | None
    domain_id: str | None = None
    job_id: str | None = None
    error_code: str | None = None


@dataclass
class TickResult:
    workflow_status: str
    projects: dict[str, ProjectState] = field(default_factory=dict)
    jobs_enqueued: list[str] = field(default_factory=list)   # job ids scheduled/linked (idempotent)
    finalized: list[tuple[str, str]] = field(default_factory=list)   # (project_id, step)
    failed: list[tuple[str, str, str | None]] = field(default_factory=list)  # (project_id, step, error)
    ran_inline: list[tuple[str, str]] = field(default_factory=list)
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

    # ------------------------------------------------------------------ helpers

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
        res = db.execute(update(ChannelWorkflow)
                         .where(ChannelWorkflow.id == workflow_id, ChannelWorkflow.status == expect.value)
                         .values(**values))
        db.commit()
        return res.rowcount == 1

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
                    res.failed.append((project_id, handler.step, out.error_code))
                    return _FAILED
                return _WAIT
            scheduled = self._schedule(db, handler, project_id, wf, now, res)
            if scheduled is None:
                return _WAIT
            domain_id, job = scheduled
            return self._reconcile(db, handler, project_id, domain_id, job, res)
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
        return self._reconcile(db, handler, project_id, domain_id, job, res)

    def _reconcile(self, db, handler, project_id: str, domain_id: str, job: PipelineJob, res: TickResult) -> str:
        """Apply what the (already linked/found) job's status means for its step."""
        if job.status == JobStatus.COMPLETED.value:
            handler.finalize(db, self.ctx, domain_id, job)
            res.finalized.append((project_id, handler.step))
            return _AGAIN
        if job.status in (JobStatus.FAILED.value, JobStatus.CANCELLED.value):
            handler.mark_failed(db, self.ctx, domain_id, job)
            res.failed.append((project_id, handler.step, job.last_error_code))
            return _FAILED
        return _WAIT  # queued / processing / waiting_capacity

    def _advance_project(self, db, project_id: str, wf: ChannelWorkflow, now, res: TickResult) -> None:
        for handler in self.chain:
            outcome = _AGAIN
            for _ in range(_MAX_STEP_PASSES):
                outcome = self._step(db, handler, project_id, wf, now, res)
                if outcome != _AGAIN:
                    break
            if outcome == _DONE:
                continue
            return  # waiting, failed, or (bounded) unresolved: nothing later may advance

    def _position(self, db, project_id: str) -> ProjectState:
        for handler in self.chain:
            db.expire_all()
            view = handler.status(db, self.ctx, db.get(StoryProject, project_id))
            if view.status is not StepStatus.COMPLETED:
                return ProjectState(handler.step, view.status, view.domain_id, view.pipeline_job_id, view.error_code)
        return ProjectState(None, None)

    # ------------------------------------------------------------------ public API

    def tick(self, workflow_id: str, *, now=None) -> TickResult:
        """recover stale jobs + reconcile in-flight steps + schedule the next missing step.
        Never dispatches. A non-ACTIVE workflow does nothing."""
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
            for pid in pids:
                self._advance_project(db, pid, wf, now, res)
            res.projects = {pid: self._position(db, pid) for pid in pids}
            failure = self._first_failure(res)
            if failure is not None:
                project_id, step, error_code = failure
                self._set_workflow_status(
                    db, workflow_id, ChannelWorkflowStatus.PAUSED, expect=ChannelWorkflowStatus.ACTIVE, now=now,
                    reason=PauseReason.STEP_FAILED,
                    detail={"project_id": project_id, "step": step, "error_code": error_code})
            elif pids and all(st.step is None for st in res.projects.values()):
                self._set_workflow_status(db, workflow_id, ChannelWorkflowStatus.FINISHED,
                                          expect=ChannelWorkflowStatus.ACTIVE, now=now)
            res.workflow_status = self._workflow(db, workflow_id).status
            return res
        finally:
            db.close()

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
        start = self._fingerprint(workflow_id)
        before = self.tick(workflow_id, now=now)
        if before.workflow_status != ChannelWorkflowStatus.ACTIVE.value:
            end = self._fingerprint(workflow_id, before.projects)
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
        after = self.tick(workflow_id, now=now)
        end = self._fingerprint(workflow_id, after.projects)
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
        """Re-arm the first non-completed step if it is FAILED. Returns the step name or None."""
        for handler in self.chain:
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

    def retry_failed_step(self, project_id: str, *, now=None) -> str | None:
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
