"""DB-backed job queue for StoryFlow (Phase 2).

Design (compare-and-set hardened against SQLite write contention):
  * Every ownership mutation runs inside BEGIN IMMEDIATE on a raw connection: SQLite
    serializes writers, so the snapshot is always current at statement time and the
    WHERE-guarded CAS returns exactly one winning row. No SELECT-then-unconditional-UPDATE.
  * Ownership of a processing job = (worker_id, claim_token) captured at claim time.
  * Capacity is DB-derived: claiming a job with a runner creates an OPEN RunnerAttempt
    (result_type IS NULL) and bumps runner.active_count. The ONLY transition that ever
    decrements active_count is idempotently CLOSING that open attempt (guarded
    UPDATE ... WHERE result_type IS NULL); repeated finalize/recovery therefore cannot
    double-decrement and active_count can never go negative (MAX(0,...) as belt).
  * PipelineJob.attempts counts BUSINESS failures only (task_failed, invalid_output).
    Quota/rate/crash/timeout/lease-expiry count against infrastructure_failures and
    never touch `attempts`. execution_count counts every dispatch (observational).
  * waiting_capacity is NOT a failure: it never bumps attempts and never sets outcome.
  * Stale recovery closes the open attempt (releasing its slot), bumps
    infrastructure_failures, requeues unless max_infra_attempts is exhausted, and
    re-checks the lease inside the guarded UPDATE.

No hidden ORM magic: nothing relies on SQLAlchemy's implicit row tracking for correctness.
"""

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from .models import (
    ACTIVE_DEDUPE_WHERE,
    ACTIVE_STATUS_VALUES,
    JobStatus,
    PipelineJob,
    RunnerAttempt,
    RunnerInstance,
    RunnerState,
    utcnow,
)
from .config import settings
from .roles import DEFAULT_ROLE


class LostOwnership(Exception):
    """The (worker_id, claim_token) pair no longer owns this job."""


class JobNotFound(Exception):
    """No row matched the requested job id and expected status."""


class RunnerUnavailable(Exception):
    """Runner is disabled or in a state that cannot claim jobs."""


class RunnerAtCapacity(Exception):
    """Runner already runs max_concurrency jobs."""


CLAIMABLE_RUNNER_STATES = (RunnerState.READY, RunnerState.BUSY)


class ImmediateTxn:
    """A single-writer BEGIN IMMEDIATE transaction on a raw DBAPI connection.

    BEGIN IMMEDIATE takes the SQLite write lock up front, so the transaction's snapshot
    is taken after every prior writer has committed. That makes the guarded CAS fully
    deterministic under concurrency (no deferred-transaction stale-snapshot surprises).
    """

    def __init__(self, engine):
        self._con = engine.raw_connection()
        self._con.isolation_level = None  # autocommit: only our explicit BEGIN counts
        self._con.execute("BEGIN IMMEDIATE")

    def execute(self, sql, params=()):
        cursor = self._con.cursor()
        cursor.execute(sql, params)
        return cursor

    def select_one(self, sql, params=()):
        cursor = self.execute(sql, params)
        return cursor.fetchone()

    def commit(self):
        self._con.commit()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        self._con.close()  # closing an uncommitted transaction rolls it back

    def close(self):
        self._con.close()


@contextmanager
def _immediate(db):
    tx = ImmediateTxn(db.get_bind())
    try:
        yield tx
        tx.commit()
    except Exception:
        tx.close()
        raise


def _fresh_job(db, job_id: str) -> PipelineJob:
    return db.scalar(select(PipelineJob).where(PipelineJob.id == job_id).execution_options(populate_existing=True))


def get_open_attempt(db, job_id: str) -> RunnerAttempt | None:
    """The currently-open RunnerAttempt for a job, or None. Read-only helper for tests."""
    return db.scalar(
        select(RunnerAttempt)
        .where(RunnerAttempt.pipeline_job_id == job_id, RunnerAttempt.result_type.is_(None))
        .order_by(RunnerAttempt.started_at)
        .limit(1)
    )


def enqueue_job(db, *, kind: str, payload=None, dedupe_key=None, priority=0,
                channel_fairness_key=None, scheduled_at=None, max_attempts=None,
                max_infra_attempts=None, session_id=None, role=None, now=None) -> PipelineJob:
    """Create a job, or return the existing active job with the same dedupe_key.

    The partial unique index (active statuses only) is the correctness backstop:
    on_conflict_do_nothing makes a concurrent duplicate insert a no-op instead of a
    duplicate row. Terminal jobs free the dedupe slot.
    """
    now = now or utcnow()
    scheduled_at = scheduled_at or now
    job_id = uuid.uuid4().hex
    inserted = db.execute(
        sqlite_insert(PipelineJob)
        .values(
            id=job_id,
            kind=kind,
            status=JobStatus.QUEUED.value,
            payload_json=payload or {},
            priority=priority,
            channel_fairness_key=channel_fairness_key,
            role=role or DEFAULT_ROLE,
            workflow_session_id=session_id,
            attempts=0,
            execution_count=0,
            infrastructure_failures=0,
            max_attempts=max_attempts or settings.max_attempts,
            max_infra_attempts=max_infra_attempts or settings.max_infra_attempts,
            scheduled_at=scheduled_at,
            dedupe_key=dedupe_key,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"], index_where=text(ACTIVE_DEDUPE_WHERE))
    )
    if inserted.rowcount == 1:
        db.commit()
        return _fresh_job(db, job_id)
    db.rollback()
    job = db.scalar(
        select(PipelineJob)
        .where(PipelineJob.dedupe_key == dedupe_key, PipelineJob.status.in_(ACTIVE_STATUS_VALUES))
        .order_by(PipelineJob.created_at)
        .limit(1)
    )
    if job is None:  # conflict on a different constraint than dedupe: surface it loudly
        raise JobNotFound(f"enqueue of {kind} conflicted but no active dedupe row found")
    return job


def claim_next_job(db, worker_id: str, lease_seconds: int, *, runner: RunnerInstance | None = None,
                   role: str | None = None, checkpoint_before=None, now=None) -> PipelineJob | None:
    """Atomically claim one due queued job for `worker_id`.

    Runs as a single BEGIN IMMEDIATE transaction: capacity check, candidate pick, and the
    WHERE status='queued' CAS all see current committed state. On success an OPEN
    RunnerAttempt is recorded (attempt lifecycle starts at dispatch) and, when a runner
    is given, its active_count is bumped. The business `attempts` counter is NOT touched.
    """
    now = now or utcnow()
    now_s = now.strftime("%Y-%m-%d %H:%M:%S.%f")
    token = uuid.uuid4().hex
    with _immediate(db) as tx:
        if runner is not None:
            row = tx.select_one(
                "SELECT enabled, state, active_count, max_concurrency, "
                "       cooldown_until, quota_reset_at FROM runner_instances WHERE id=?",
                (runner.id,),
            )
            if row is None or not row[0]:
                raise RunnerUnavailable(
                    f"runner {runner.id} cannot claim (enabled={row[0] if row else None})"
                )
            state = row[1]
            claimable = state in tuple(s.value for s in CLAIMABLE_RUNNER_STATES) or (
                state in (RunnerState.COOLDOWN.value, RunnerState.RATE_LIMITED.value)
                and row[4] is not None and row[4] <= now_s
            ) or (
                state == RunnerState.QUOTA_EXHAUSTED.value
                and row[5] is not None and row[5] <= now_s
            )
            if not claimable:
                raise RunnerUnavailable(f"runner {runner.id} cannot claim (state={state})")
            if row[2] >= row[3]:
                raise RunnerAtCapacity(f"runner {runner.id} at capacity {row[2]}/{row[3]}")
        job_id = tx.select_one(
            "SELECT id FROM pipeline_jobs WHERE status=? AND scheduled_at<=? "
            "ORDER BY priority DESC, scheduled_at, created_at LIMIT 1",
            (JobStatus.QUEUED.value, now),
        )
        if job_id is None:
            return None
        job_id = job_id[0]
        cursor = tx.execute(
            "UPDATE pipeline_jobs SET status=?, worker_id=?, claim_token=?, started_at=?, "
            "lease_expires_at=?, execution_count=execution_count+1, updated_at=? "
            "WHERE id=? AND status=?",
            (
                JobStatus.PROCESSING.value,
                worker_id,
                token,
                now,
                now + timedelta(seconds=lease_seconds),
                now,
                job_id,
                JobStatus.QUEUED.value,
            ),
        )
        if cursor.rowcount != 1:
            return None  # lost the CAS; another writer already took it
        if runner is not None:
            tx.execute(
                "UPDATE runner_instances SET active_count=active_count+1, state=?, last_used_at=?, "
                "last_health_at=? WHERE id=?",
                (RunnerState.BUSY.value, now, now, runner.id),
            )
        job_role, exec_count = tx.select_one(
            "SELECT role, execution_count FROM pipeline_jobs WHERE id=?", (job_id,)
        )
        tx.execute(
            "INSERT INTO runner_attempts "
            "(id, pipeline_job_id, runner_instance_id, role, attempt_number, started_at, "
            " finished_at, result_type, checkpoint_before, checkpoint_after, error_code, error_message) "
            "VALUES (?,?,?,?,?,?,NULL,NULL,?,NULL,NULL,NULL)",
            (
                uuid.uuid4().hex,
                job_id,
                runner.id if runner is not None else None,
                role or job_role or DEFAULT_ROLE,
                exec_count,
                now,
                json.dumps(checkpoint_before) if checkpoint_before is not None else None,
            ),
        )
    return _fresh_job(db, job_id)


def renew_lease(db, job_id: str, worker_id: str, claim_token: str, lease_seconds: int, *, now=None) -> bool:
    """Extend the lease only while this exact owner still holds the processing job."""
    now = now or utcnow()
    with _immediate(db) as tx:
        cursor = tx.execute(
            "UPDATE pipeline_jobs SET lease_expires_at=?, updated_at=? "
            "WHERE id=? AND status=? AND worker_id=? AND claim_token=?",
            (now + timedelta(seconds=lease_seconds), now, job_id, JobStatus.PROCESSING.value, worker_id, claim_token),
        )
        owns = cursor.rowcount == 1
    return owns


def _close_open_attempt(tx, job_id: str, now, *, result_type, error_code=None, error_message=None,
                        checkpoint_before=None, checkpoint_after=None,
                        runner_state=RunnerState.READY, extra=None):
    """Idempotently close the job's open attempt and release its capacity slot.

    Closing is guarded by `result_type IS NULL`, so at most one call decrements
    runner.active_count per attempt. `extra` is an optional (column, value) write to the
    runner (cooldown_until / quota_reset_at).
    """
    att = tx.select_one(
        "SELECT id, runner_instance_id FROM runner_attempts "
        "WHERE pipeline_job_id=? AND result_type IS NULL ORDER BY started_at LIMIT 1",
        (job_id,),
    )
    if att is None:
        return
    cols = "result_type=?, finished_at=?, error_code=?, error_message=?"
    params = [result_type, now, error_code, error_message]
    if checkpoint_before is not None:
        cols += ", checkpoint_before=?"
        params.append(json.dumps(checkpoint_before))
    if checkpoint_after is not None:
        cols += ", checkpoint_after=?"
        params.append(json.dumps(checkpoint_after))
    params.append(att[0])
    cursor = tx.execute(f"UPDATE runner_attempts SET {cols} WHERE id=? AND result_type IS NULL", tuple(params))
    if cursor.rowcount != 1:
        return
    runner_id = att[1]
    if runner_id:
        if extra is not None:
            col, value = extra
            tx.execute(
                f"UPDATE runner_instances SET active_count=MAX(0, active_count-1), state=?, "
                f"last_health_at=?, {col}=? WHERE id=?",
                (runner_state.value, now, value, runner_id),
            )
        else:
            tx.execute(
                "UPDATE runner_instances SET active_count=MAX(0, active_count-1), state=?, "
                "last_health_at=? WHERE id=?",
                (runner_state.value, now, runner_id),
            )


def _transition(db, job_id: str, worker_id: str, claim_token: str, now, *, effects_sql: str,
                effects_params: tuple, attempt_result: str | None, attempt_error_code=None,
                attempt_error_message=None, checkpoint_before=None, checkpoint_after=None,
                runner_state=RunnerState.READY, runner_extra=None) -> PipelineJob:
    """Owner-guarded status transition: `effects_sql`/`effects_params` apply only if
    (worker_id, claim_token) still owns a processing job. Failure -> LostOwnership.
    The open RunnerAttempt is closed in the same transaction (releasing its slot)."""
    with _immediate(db) as tx:
        cursor = tx.execute(
            "UPDATE pipeline_jobs SET " + effects_sql + ", updated_at=? "
            "WHERE id=? AND status=? AND worker_id=? AND claim_token=?",
            (*effects_params, now, job_id, JobStatus.PROCESSING.value, worker_id, claim_token),
        )
        if cursor.rowcount != 1:
            raise LostOwnership(job_id)
        if attempt_result is not None:
            _close_open_attempt(
                tx, job_id, now,
                result_type=attempt_result, error_code=attempt_error_code, error_message=attempt_error_message,
                checkpoint_before=checkpoint_before, checkpoint_after=checkpoint_after,
                runner_state=runner_state, extra=runner_extra,
            )
    return _fresh_job(db, job_id)


def _started_job(db, job_id: str, kind: str):
    job = _fresh_job(db, job_id)
    if job is None:
        raise JobNotFound(f"{kind}: job {job_id} not found")
    return job


def complete_job(db, job_id, worker_id, claim_token, *, outcome=None, checkpoint_before=None,
                 checkpoint_after=None, now=None) -> PipelineJob:
    """Business success: closes the open attempt (releases its capacity slot)."""
    now = now or utcnow()
    _started_job(db, job_id, "complete_job")
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?, finished_at=?, outcome=?, worker_id=NULL, claim_token=NULL, "
                    "lease_expires_at=NULL, last_error_code=NULL, last_error_message=NULL",
        effects_params=(JobStatus.COMPLETED.value, now, outcome or "success"),
        attempt_result="success",
        checkpoint_before=checkpoint_before, checkpoint_after=checkpoint_after,
    )


def fail_job(db, job_id, worker_id, claim_token, *, error_code=None, error_message=None, outcome=None,
             checkpoint_before=None, checkpoint_after=None, now=None) -> PipelineJob:
    """Terminal business failure; the job will not be retried by this call."""
    now = now or utcnow()
    _started_job(db, job_id, "fail_job")
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?, finished_at=?, outcome=?, last_error_code=?, last_error_message=?, "
                    "worker_id=NULL, claim_token=NULL, lease_expires_at=NULL, started_at=NULL",
        effects_params=(JobStatus.FAILED.value, now, outcome or "failed", error_code, error_message),
        attempt_result="task_failed", attempt_error_code=error_code, attempt_error_message=error_message,
        checkpoint_before=checkpoint_before, checkpoint_after=checkpoint_after,
    )


def business_failure(db, job_id, worker_id, claim_token, *, result_type="task_failed",
                     error_code=None, error_message=None, delay_seconds=0,
                     checkpoint_before=None, checkpoint_after=None, now=None) -> PipelineJob:
    """A real business failure: bumps PipelineJob.attempts, then requeues for retry or
    fails the job once max_attempts is reached. Never called for infra-only events."""
    now = now or utcnow()
    job = _started_job(db, job_id, "business_failure")
    exhausted = (job.attempts or 0) + 1 >= (job.max_attempts or settings.max_attempts)
    common = dict(
        attempt_result=result_type, attempt_error_code=error_code, attempt_error_message=error_message,
        checkpoint_before=checkpoint_before, checkpoint_after=checkpoint_after,
    )
    if exhausted:
        return _transition(
            db, job_id, worker_id, claim_token, now,
            effects_sql="status=?, finished_at=?, outcome=?, attempts=attempts+1, "
                        "last_error_code=?, last_error_message=?, worker_id=NULL, "
                        "claim_token=NULL, lease_expires_at=NULL, started_at=NULL",
            effects_params=(JobStatus.FAILED.value, now, "failed", error_code, error_message),
            **common,
        )
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?, scheduled_at=?, attempts=attempts+1, started_at=NULL, finished_at=NULL, "
                    "outcome=NULL, last_error_code=?, last_error_message=?, worker_id=NULL, "
                    "claim_token=NULL, lease_expires_at=NULL",
        effects_params=(JobStatus.QUEUED.value, now + timedelta(seconds=delay_seconds), error_code, error_message),
        **common,
    )


def infra_failure(db, job_id, worker_id, claim_token, *, result_type, error_code=None, error_message=None,
                  delay_seconds=0, runner_state=RunnerState.READY, runner_extra=None,
                  checkpoint_before=None, checkpoint_after=None, now=None) -> PipelineJob:
    """An infrastructure event (crash/timeout/transient): bumps infrastructure_failures,
    requeues for another infrastructure attempt or fails once max_infra_attempts is hit.
    Business `attempts` is untouched."""
    now = now or utcnow()
    job = _started_job(db, job_id, "infra_failure")
    exhausted = (job.infrastructure_failures or 0) + 1 >= (
        job.max_infra_attempts or settings.max_infra_attempts
    )
    common = dict(
        attempt_result=result_type, attempt_error_code=error_code or result_type,
        attempt_error_message=error_message, checkpoint_before=checkpoint_before,
        checkpoint_after=checkpoint_after, runner_state=runner_state, runner_extra=runner_extra,
    )
    if exhausted:
        return _transition(
            db, job_id, worker_id, claim_token, now,
            effects_sql="status=?, finished_at=?, outcome=?, infrastructure_failures=infrastructure_failures+1, "
                        "last_error_code=?, last_error_message=?, worker_id=NULL, "
                        "claim_token=NULL, lease_expires_at=NULL, started_at=NULL",
            effects_params=(JobStatus.FAILED.value, now, "failed", "INFRA_EXHAUSTED", error_message),
            **common,
        )
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?, scheduled_at=?, infrastructure_failures=infrastructure_failures+1, "
                    "started_at=NULL, finished_at=NULL, outcome=NULL, "
                    "last_error_code=?, last_error_message=?, worker_id=NULL, "
                    "claim_token=NULL, lease_expires_at=NULL",
        effects_params=(
            JobStatus.QUEUED.value,
            now + timedelta(seconds=delay_seconds),
            error_code or result_type,
            error_message,
        ),
        **common,
    )


def rate_limited(db, job_id, worker_id, claim_token, *, retry_after=0,
                 error_message=None, now=None) -> PipelineJob:
    """Rate limit: NOT a business failure. Runner goes to cooldown, its slot is freed,
    and the job is requeued after the cooldown window."""
    now = now or utcnow()
    cooldown_until = now + timedelta(seconds=max(retry_after, 0))
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?, scheduled_at=?, started_at=NULL, finished_at=NULL, outcome=NULL, "
                    "last_error_code=?, last_error_message=?, worker_id=NULL, "
                    "claim_token=NULL, lease_expires_at=NULL",
        effects_params=(JobStatus.QUEUED.value, cooldown_until, "rate_limited", error_message),
        attempt_result="rate_limited", attempt_error_code="rate_limited", attempt_error_message=error_message,
        runner_state=RunnerState.COOLDOWN, runner_extra=("cooldown_until", cooldown_until),
    )


def quota_exhausted(db, job_id, worker_id, claim_token, *, quota_reset_at=None,
                    error_message=None, now=None) -> PipelineJob:
    """Quota exhaustion: NOT a business failure. Runner is parked with a quota_reset_at,
    its slot is freed, and the job is requeued immediately for failover dispatch."""
    now = now or utcnow()
    reset_at = quota_reset_at or now + timedelta(seconds=settings.quota_reset_seconds)
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?, scheduled_at=?, started_at=NULL, finished_at=NULL, outcome=NULL, "
                    "last_error_code=?, last_error_message=?, worker_id=NULL, "
                    "claim_token=NULL, lease_expires_at=NULL",
        effects_params=(JobStatus.QUEUED.value, now, "quota_exhausted", error_message),
        attempt_result="quota_exhausted", attempt_error_code="quota_exhausted", attempt_error_message=error_message,
        runner_state=RunnerState.QUOTA_EXHAUSTED, runner_extra=("quota_reset_at", reset_at),
    )


def auth_error(db, job_id, worker_id, claim_token, *, error_message=None, now=None) -> PipelineJob:
    """Auth failure: runner removed from candidates until an operator revives it.
    Job requeued; if nothing else can run it the dispatcher parks/fails it."""
    now = now or utcnow()
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?, scheduled_at=?, started_at=NULL, finished_at=NULL, outcome=NULL, "
                    "last_error_code=?, last_error_message=?, worker_id=NULL, "
                    "claim_token=NULL, lease_expires_at=NULL",
        effects_params=(JobStatus.QUEUED.value, now, "auth_error", error_message),
        attempt_result="auth_error", attempt_error_code="auth_error", attempt_error_message=error_message,
        runner_state=RunnerState.AUTH_ERROR,
    )


def cancel_job(db, job_id, worker_id, claim_token, *, error_message=None, now=None) -> PipelineJob:
    """Client cancellation (often from RunnerResult(code=CANCELLED))."""
    now = now or utcnow()
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?, finished_at=?, outcome=?, last_error_code=?, last_error_message=?, "
                    "worker_id=NULL, claim_token=NULL, lease_expires_at=NULL, started_at=NULL",
        effects_params=(JobStatus.CANCELLED.value, now, "cancelled", "cancelled", error_message),
        attempt_result="cancelled", attempt_error_code="cancelled", attempt_error_message=error_message,
    )


def move_to_waiting_capacity(db, job_id, worker_id, claim_token, *, now=None) -> PipelineJob:
    """Processing -> waiting_capacity while holding ownership. NOT a business failure:
    attempts/outcome/error are untouched. Closes the current attempt (releases its slot)."""
    now = now or utcnow()
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?",
        effects_params=(JobStatus.WAITING_CAPACITY.value,),
        attempt_result="parked",
    )


def park_job(db, job_id, *, now=None, error_message=None) -> PipelineJob:
    """Queued -> waiting_capacity (no owner yet): used when the dispatcher finds no
    eligible runner under pause_auto_resume. No attempt exists to close."""
    now = now or utcnow()
    with _immediate(db) as tx:
        cursor = tx.execute(
            "UPDATE pipeline_jobs SET status=?, last_error_code=?, last_error_message=?, updated_at=? "
            "WHERE id=? AND status=?",
            (JobStatus.WAITING_CAPACITY.value, "all_agents_unavailable", error_message, now, job_id, JobStatus.QUEUED.value),
        )
        if cursor.rowcount != 1:
            raise JobNotFound(job_id)
    return _fresh_job(db, job_id)


def fail_all_agents_unavailable(db, job_id, *, now=None, error_message=None) -> PipelineJob:
    """Queued -> failed when the session policy is require_attention and no eligible
    runner exists. Intentionally terminal: a human has to re-queue."""
    now = now or utcnow()
    with _immediate(db) as tx:
        cursor = tx.execute(
            "UPDATE pipeline_jobs SET status=?, finished_at=?, outcome=?, last_error_code=?, "
            "last_error_message=?, updated_at=? WHERE id=? AND status=?",
            (
                JobStatus.FAILED.value, now, "failed", "ALL_AGENTS_UNAVAILABLE",
                error_message or "no eligible runner; require_attention policy", now, job_id, JobStatus.QUEUED.value,
            ),
        )
        if cursor.rowcount != 1:
            raise JobNotFound(job_id)
    return _fresh_job(db, job_id)


def promote_waiting_capacity(db, job_id, *, now=None) -> PipelineJob:
    """waiting_capacity -> queued so it becomes claimable again. Used by the dispatcher
    to auto-resume jobs once an eligible runner is available (pause_auto_resume)."""
    now = now or utcnow()
    with _immediate(db) as tx:
        cursor = tx.execute(
            "UPDATE pipeline_jobs SET status=?, scheduled_at=?, updated_at=? WHERE id=? AND status=?",
            (JobStatus.QUEUED.value, now, now, job_id, JobStatus.WAITING_CAPACITY.value),
        )
        if cursor.rowcount != 1:
            raise JobNotFound(job_id)
    return _fresh_job(db, job_id)


def recover_stale_jobs(db, *, now=None) -> int:
    """Recover only processing jobs whose lease has actually expired.

    For each recovered job the open RunnerAttempt is closed (releasing its runner slot —
    the Phase 1 debt), infrastructure_failures is bumped, and the job is failed when
    max_infra_attempts is exhausted, else requeued. Business `attempts` is untouched.
    Each guarded UPDATE re-checks status + lease in the WHERE clause, so an attempt
    renewed between scan and update is skipped and nothing is decremented twice.
    """
    now = now or utcnow()
    recovered = 0
    with _immediate(db) as tx:
        ids = [r[0] for r in tx.execute(
            "SELECT id FROM pipeline_jobs WHERE status=? AND lease_expires_at IS NOT NULL AND lease_expires_at<? "
            "ORDER BY lease_expires_at",
            (JobStatus.PROCESSING.value, now),
        ).fetchall()]
        for job_id in ids:
            row = tx.select_one(
                "SELECT infrastructure_failures, max_infra_attempts FROM pipeline_jobs WHERE id=?",
                (job_id,),
            )
            if row is None:
                continue
            infra, max_infra = row
            _close_open_attempt(
                tx, job_id, now,
                result_type="stale", error_code="LEASE_EXPIRED",
                error_message="recovered after lease expiry",
            )
            exhausted = (infra or 0) + 1 >= (max_infra or settings.max_infra_attempts)
            cursor = tx.execute(
                "UPDATE pipeline_jobs SET status=?, finished_at=?, scheduled_at=?, outcome=?, "
                "infrastructure_failures=infrastructure_failures+1, last_error_code=?, "
                "last_error_message=?, worker_id=NULL, claim_token=NULL, lease_expires_at=NULL, "
                "started_at=NULL, updated_at=? "
                "WHERE id=? AND status=? AND lease_expires_at IS NOT NULL AND lease_expires_at<?",
                (
                    JobStatus.FAILED.value if exhausted else JobStatus.QUEUED.value,
                    now if exhausted else None,
                    now,
                    "failed" if exhausted else None,
                    "LEASE_EXPIRED",
                    "job lease expired; recovered by stale-job recovery",
                    now,
                    job_id,
                    JobStatus.PROCESSING.value,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                continue  # a heartbeat renewed the lease since the scan; attempt close rolled back
            recovered += 1
    return recovered