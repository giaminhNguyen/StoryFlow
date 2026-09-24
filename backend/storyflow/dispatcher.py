"""Dispatcher: picks a runner for a job and applies the runner's result.

Selection (V1, fully deterministic, no AI):
  candidates = session allow-list ∩ enabled ∩ registered ∩ state-now-eligible
               ∩ role-compatible ∩ free concurrency
  ranked by (role preference index if configured, normalized load, active_count tie,
             last-used (LRU), runner_type, id).

Allowed transition decisions:
  * success            -> complete_job
  * task_failed/invalid_output -> business_failure (bumps PipelineJob.attempts)
  * transient_failure  -> infra_failure (requeue)
  * rate_limited       -> runner cooldown + requeue after cooldown
  * quota_exhausted    -> runner quota_exhausted + requeue now (failover loop in run_round)
  * auth_error         -> runner auth_error + requeue
  * runner_crashed/timeout -> infra_failure (bumps infrastructure_failures)
  * cancelled          -> cancel_job
  * no eligible candidate -> pause_auto_resume -> waiting_capacity park
                            require_attention   -> terminal fail (needs human)

An unselected runner (outside the session allow-list) can never receive a job, even
transiently: failover goes back through the exact same candidate filter.
"""

import enum
from datetime import datetime

from sqlalchemy import select

from . import queue
from .agents import RunnerRegistry
from .config import settings
from .models import JobStatus, PipelineJob, RunnerInstance, RunnerState, WorkflowSession, utcnow
from .protocol import ResultCode, RunnerResult, TaskPacket
from .roles import DEFAULT_ROLE

_MIN_DT = datetime.min

# The queue persists through raw SQL inside BEGIN IMMEDIATE; sessions here use
# expire_on_commit=False, so ORM reads must bypass the identity map to see
# post-commit state.
_FRESH = {"execution_options": {"populate_existing": True}}


class DispatchOutcome(str, enum.Enum):
    DISPATCHED_SUCCESS = "dispatched_success"
    DISPATCHED_REQUEUED = "dispatched_requeued"          # business/infra retry queued
    DISPATCHED_FAILED = "dispatched_failed"
    DISPATCHED_CANCELLED = "dispatched_cancelled"
    DISPATCHED_QUOTA = "dispatched_quota"                # requeued; keep failing over
    DISPATCHED_RATE_LIMITED = "dispatched_rate_limited"
    PARKED_NO_CANDIDATE = "parked_no_candidate"
    FAILED_NO_CANDIDATE = "failed_no_candidate"
    LOST_RACE = "lost_race"
    NOTHING_TO_DO = "nothing_to_do"


def sanitize_error(message, limit=400):
    """Bounded, secret-free error text for storage in RunnerAttempt/job diagnostics."""
    if not message:
        return None
    return str(message)[:limit]


def build_task_packet(job: PipelineJob, runner: RunnerInstance, *, task_id: str, role: str | None = None,
                      checkpoint_before=None) -> TaskPacket:
    payload = job.payload_json or {}
    skill = payload.get("skill") or {}
    return TaskPacket(
        task_id=task_id,
        job_id=job.id,
        role=role or job.role or DEFAULT_ROLE,
        skill_name=skill.get("name"),
        skill_path=skill.get("path"),
        skill_revision=skill.get("revision"),
        workspace_root=payload.get("workspace_root"),
        write_scope=payload.get("write_scope", "workspace"),
        inputs=payload.get("inputs") or {},
        outputs=payload.get("outputs") or [],
        task_config=payload.get("task_config") or {},
        constraints=payload.get("constraints") or {},
    )


class Dispatcher:
    def __init__(self, registry: RunnerRegistry, *, lease_seconds=None, cooldown_seconds=None,
                 quota_reset_seconds=None, max_failover_passes=None):
        self.registry = registry
        self.lease_seconds = lease_seconds or settings.lease_seconds
        self.cooldown_seconds = cooldown_seconds or settings.cooldown_seconds
        self.quota_reset_seconds = quota_reset_seconds or settings.quota_reset_seconds
        self.max_failover_passes = max_failover_passes or settings.max_failover_passes

    # --- selection ----------------------------------------------------------

    def _state_can_take(self, runner: RunnerInstance, now: datetime) -> bool:
        if not runner.enabled or runner.state in (RunnerState.OFFLINE.value, RunnerState.DISABLED.value,
                                                  RunnerState.AUTH_ERROR.value):
            return False
        if runner.state == RunnerState.QUOTA_EXHAUSTED.value:
            return runner.quota_reset_at is not None and now >= runner.quota_reset_at
        if runner.state in (RunnerState.COOLDOWN.value, RunnerState.RATE_LIMITED.value):
            return runner.cooldown_until is not None and now >= runner.cooldown_until
        return True

    def candidates(self, db, session: WorkflowSession, role: str, now: datetime) -> list[RunnerInstance]:
        """Ranked eligible runners for `role` within the session allow-list only."""
        prefs = (session.role_preferences or {}).get(role, [])
        rows = db.scalars(
            select(RunnerInstance).where(RunnerInstance.workflow_session_id == session.id),
            **_FRESH,
        ).all()
        pool = []
        for r in rows:
            if not r.enabled or not self.registry.has(r.id):
                continue
            if not self._state_can_take(r, now):
                continue
            if role not in (r.supported_roles or []):
                continue
            if (r.active_count or 0) >= max(r.max_concurrency or 1, 1):
                continue
            pool.append(r)

        def rank(runner: RunnerInstance):
            pref_idx = prefs.index(runner.runner_type) if runner.runner_type in prefs else len(prefs)
            load = (runner.active_count or 0) / max(runner.max_concurrency or 1, 1)
            last_used = runner.last_used_at or _MIN_DT
            return (pref_idx, load, runner.active_count or 0, last_used, runner.runner_type, runner.id)

        pool.sort(key=rank)
        return pool

    # --- main loop ----------------------------------------------------------

    def _next_queued_job(self, db, session: WorkflowSession, now: datetime) -> PipelineJob | None:
        job_id = db.scalar(
            select(PipelineJob.id)
            .where(PipelineJob.status == JobStatus.QUEUED.value,
                   PipelineJob.workflow_session_id == session.id,
                   PipelineJob.scheduled_at <= now)
            .order_by(PipelineJob.priority.desc(), PipelineJob.scheduled_at, PipelineJob.created_at)
            .limit(1),
            **_FRESH,
        )
        return db.get(PipelineJob, job_id) if job_id else None

    def resume_waiting(self, db, session: WorkflowSession, now: datetime, *, limit=1) -> int:
        """Promote waiting_capacity jobs back to queued once eligibility exists.
        One per call by default: promotion is event-driven, not a busy loop."""
        waiting = db.scalars(
            select(PipelineJob)
            .where(PipelineJob.status == JobStatus.WAITING_CAPACITY.value,
                   PipelineJob.workflow_session_id == session.id)
            .order_by(PipelineJob.priority.desc(), PipelineJob.scheduled_at, PipelineJob.created_at)
            .limit(max(limit, 1)),
            **_FRESH,
        ).all()
        promoted = 0
        for job in waiting:
            role = job.role or DEFAULT_ROLE
            if self.candidates(db, session, role, now):
                queue.promote_waiting_capacity(db, job.id, now=now)
                promoted += 1
        return promoted

    def run_round(self, db, session: WorkflowSession, *, now: datetime | None = None) -> tuple[DispatchOutcome, str | None]:
        """One unit of work: resume, pick the next due job, dispatch with bounded quota
        failover. Returns (outcome, job_id)."""
        now = now or utcnow()
        self.resume_waiting(db, session, now)
        job = self._next_queued_job(db, session, now)
        if job is None:
            return DispatchOutcome.NOTHING_TO_DO, None
        outcome = DispatchOutcome.NOTHING_TO_DO
        for _ in range(max(1, self.max_failover_passes)):
            outcome = self.dispatch_job(db, session, job, now)
            if outcome != DispatchOutcome.DISPATCHED_QUOTA:
                break
            job = self._next_queued_job(db, session, now)
            if job is None:
                break
        return outcome, job.id

    # --- dispatch -----------------------------------------------------------

    def dispatch_job(self, db, session: WorkflowSession, job: PipelineJob, now: datetime) -> DispatchOutcome:
        role = job.role or DEFAULT_ROLE
        candidates = self.candidates(db, session, role, now)
        if not candidates:
            return self._no_candidate(db, session, job, now)
        runner = candidates[0]
        agent = self.registry.get(runner.id)
        claimed = queue.claim_next_job(
            db, runner.id, self.lease_seconds, runner=runner, role=role,
            checkpoint_before=(job.payload_json or {}).get("checkpoint_before"), now=now,
        )
        if claimed is None:
            return DispatchOutcome.LOST_RACE
        attempt = queue.get_open_attempt(db, claimed.id)
        task_id = f"{claimed.id}:{attempt.attempt_number}" if attempt else f"{claimed.id}"
        packet = build_task_packet(claimed, runner, task_id=task_id, role=role)
        try:
            result = agent.execute(packet)
        except BaseException as exc:  # runner crashed / timed out mid-execution
            code = agent.classify_error(exc)
            result = RunnerResult(code=code, error_message=sanitize_error(str(exc)))
        return self._apply_result(db, claimed, runner, result, now)

    def _no_candidate(self, db, session: WorkflowSession, job: PipelineJob, now: datetime) -> DispatchOutcome:
        policy = (session.all_agents_unavailable_policy or "pause_auto_resume")
        if policy == "require_attention":
            try:
                queue.fail_all_agents_unavailable(db, job.id, now=now)
            except queue.JobNotFound:
                return DispatchOutcome.LOST_RACE
            return DispatchOutcome.FAILED_NO_CANDIDATE
        try:
            queue.park_job(db, job.id, now=now)
        except queue.JobNotFound:
            return DispatchOutcome.LOST_RACE
        return DispatchOutcome.PARKED_NO_CANDIDATE

    def apply_result(self, db, job: PipelineJob, runner: RunnerInstance, result: RunnerResult,
                     now: datetime | None = None) -> DispatchOutcome:
        return self._apply_result(db, job, runner, result, now or utcnow())

    def _apply_result(self, db, job: PipelineJob, runner: RunnerInstance, result: RunnerResult,
                      now: datetime) -> DispatchOutcome:
        wid = runner.id
        tok = job.claim_token
        msg = sanitize_error(result.error_message)
        code = result.code
        if code is ResultCode.SUCCESS:
            queue.complete_job(db, job.id, wid, tok, outcome="success",
                               checkpoint_before=result.checkpoint_before,
                               checkpoint_after=result.checkpoint_after, now=now)
            return DispatchOutcome.DISPATCHED_SUCCESS
        if code in (ResultCode.TASK_FAILED, ResultCode.INVALID_OUTPUT):
            queue.business_failure(
                db, job.id, wid, tok, result_type=code.value,
                error_code=result.error_code or code.value, error_message=msg,
                checkpoint_before=result.checkpoint_before, checkpoint_after=result.checkpoint_after, now=now,
            )
            return DispatchOutcome.DISPATCHED_REQUEUED
        if code is ResultCode.TRANSIENT_FAILURE:
            queue.infra_failure(db, job.id, wid, tok, result_type="transient_failure",
                                error_code=result.error_code or "transient_failure", error_message=msg,
                                delay_seconds=result.retry_after or 0,
                                checkpoint_after=result.checkpoint_after, now=now)
            return DispatchOutcome.DISPATCHED_REQUEUED
        if code is ResultCode.RATE_LIMITED:
            queue.rate_limited(db, job.id, wid, tok,
                               retry_after=result.retry_after or self.cooldown_seconds,
                               error_message=msg, now=now)
            return DispatchOutcome.DISPATCHED_RATE_LIMITED
        if code is ResultCode.QUOTA_EXHAUSTED:
            queue.quota_exhausted(
                db, job.id, wid, tok,
                quota_reset_at=result.quota_reset_at or now + _seconds(self.quota_reset_seconds),
                error_message=msg, now=now,
            )
            return DispatchOutcome.DISPATCHED_QUOTA
        if code is ResultCode.AUTH_ERROR:
            queue.auth_error(db, job.id, wid, tok, error_message=msg, now=now)
            return DispatchOutcome.DISPATCHED_REQUEUED
        if code in (ResultCode.RUNNER_CRASHED, ResultCode.TIMEOUT):
            queue.infra_failure(db, job.id, wid, tok, result_type=code.value,
                                error_code=result.error_code or code.value, error_message=msg,
                                checkpoint_after=result.checkpoint_after, now=now)
            return DispatchOutcome.DISPATCHED_REQUEUED
        if code is ResultCode.CANCELLED:
            queue.cancel_job(db, job.id, wid, tok, error_message=msg, now=now)
            return DispatchOutcome.DISPATCHED_CANCELLED
        raise ValueError(f"unhandled result code {code}")


def _seconds(n):
    from datetime import timedelta
    return timedelta(seconds=n)