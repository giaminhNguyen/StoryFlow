"""DB-backed job queue for StoryFlow Phase 1.

Design (compare-and-set hardened against SQLite write contention):
  * Every ownership mutation runs inside BEGIN IMMEDIATE on a raw connection: SQLite
    serializes writers, so the snapshot is always current at statement time and the
    WHERE-guarded CAS returns exactly one winning row. No SELECT-then-unconditional-UPDATE.
  * Ownership of a processing job = (worker_id, claim_token) captured at claim time.
    A job may only be finalized by those exact values.
  * waiting_capacity is NOT a business failure: the transition never bumps the
    attempts counter and never touches outcome/error.
  * Stale recovery only touches processing jobs whose lease has actually expired and
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

    def insert_each(self, table, rows):
        for row in rows:
            cols = ",".join(row.keys())
            marks = ",".join(["?"] * len(row))
            self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))

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


def _attempt_values(job, now, *, result_type, runner=None, role=None, error_code=None,
                    error_message=None, checkpoint_before=None, checkpoint_after=None, extra=None) -> dict:
    row = {
        "id": uuid.uuid4().hex,
        "pipeline_job_id": job.id,
        "runner_instance_id": runner.id if runner is not None else None,
        "role": role or settings.default_role,
        "attempt_number": job.attempts,
        "started_at": job.started_at,
        "finished_at": now,
        "result_type": result_type,
        "checkpoint_before": json.dumps(checkpoint_before) if checkpoint_before is not None else None,
        "checkpoint_after": json.dumps(checkpoint_after) if checkpoint_after is not None else None,
        "error_code": error_code,
        "error_message": error_message,
    }
    if extra:
        row.update(extra)
    return row


def enqueue_job(db, *, kind: str, payload=None, dedupe_key=None, priority=0,
                channel_fairness_key=None, scheduled_at=None, max_attempts=None,
                now=None) -> PipelineJob:
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
            attempts=0,
            max_attempts=max_attempts or settings.max_attempts,
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
                   now=None) -> PipelineJob | None:
    """Atomically claim one due queued job for `worker_id`.

    Runs as a single BEGIN IMMEDIATE transaction: capacity check, candidate pick, and the
    WHERE status='queued' CAS all see current committed state. Two racing workers can never
    both claim the same job.
    """
    now = now or utcnow()
    token = uuid.uuid4().hex
    with _immediate(db) as tx:
        if runner is not None:
            row = tx.select_one(
                "SELECT enabled, state, active_count, max_concurrency FROM runner_instances WHERE id=?",
                (runner.id,),
            )
            if row is None or not row[0] or row[1] not in tuple(s.value for s in CLAIMABLE_RUNNER_STATES):
                raise RunnerUnavailable(f"runner {runner.id} cannot claim (enabled={row[0] if row else None}, state={row[1] if row else None})")
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
            "lease_expires_at=?, attempts=attempts+1, updated_at=? WHERE id=? AND status=?",
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
                "UPDATE runner_instances SET active_count=active_count+1, state=?, last_health_at=? WHERE id=?",
                (RunnerState.BUSY.value, now, runner.id),
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


def _transition(db, job_id: str, worker_id: str, claim_token: str, now, *, effects_sql: str,
                effects_params: tuple, attempt: dict | None, runner: RunnerInstance | None) -> PipelineJob:
    """Owner-guarded status transition: `effects_sql`/`effects_params` apply only if
    (worker_id, claim_token) still owns a processing job. Failure -> LostOwnership."""
    with _immediate(db) as tx:
        cursor = tx.execute(
            "UPDATE pipeline_jobs SET " + effects_sql + ", updated_at=? "
            "WHERE id=? AND status=? AND worker_id=? AND claim_token=?",
            (*effects_params, now, job_id, JobStatus.PROCESSING.value, worker_id, claim_token),
        )
        if cursor.rowcount != 1:
            raise LostOwnership(job_id)
        if attempt is not None:
            tx.insert_each("runner_attempts", [attempt])
        if runner is not None:
            tx.execute(
                "UPDATE runner_instances SET active_count=MAX(0, active_count-1), state=?, last_health_at=? WHERE id=?",
                (RunnerState.READY.value, now, runner.id),
            )
    return _fresh_job(db, job_id)


def _started_job(db, job_id: str, kind: str):
    job = _fresh_job(db, job_id)
    if job is None:
        raise JobNotFound(f"{kind}: job {job_id} not found")
    return job


def complete_job(db, job_id, worker_id, claim_token, *, outcome=None, runner=None, role=None,
                 checkpoint_before=None, checkpoint_after=None, now=None) -> PipelineJob:
    now = now or utcnow()
    job = _started_job(db, job_id, "complete_job")
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?, finished_at=?, outcome=?, worker_id=NULL, claim_token=NULL, "
                    "lease_expires_at=NULL, last_error_code=NULL, last_error_message=NULL",
        effects_params=(JobStatus.COMPLETED.value, now, outcome),
        attempt=_attempt_values(job, now, result_type=outcome or "success", runner=runner, role=role,
                                checkpoint_before=checkpoint_before, checkpoint_after=checkpoint_after),
        runner=runner,
    )


def fail_job(db, job_id, worker_id, claim_token, *, error_code=None, error_message=None, outcome=None,
             runner=None, role=None, checkpoint_before=None, checkpoint_after=None, now=None) -> PipelineJob:
    """Terminal failure (the job will not be retried by this call)."""
    now = now or utcnow()
    job = _started_job(db, job_id, "fail_job")
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?, finished_at=?, outcome=?, last_error_code=?, last_error_message=?, "
                    "worker_id=NULL, claim_token=NULL, lease_expires_at=NULL, started_at=NULL",
        effects_params=(JobStatus.FAILED.value, now, outcome or "failed", error_code, error_message),
        attempt=_attempt_values(job, now, result_type="failed", runner=runner, role=role,
                                error_code=error_code, error_message=error_message,
                                checkpoint_before=checkpoint_before, checkpoint_after=checkpoint_after),
        runner=runner,
    )


def requeue_job(db, job_id, worker_id, claim_token, *, error_code=None, error_message=None,
                delay_seconds=0, runner=None, role=None, checkpoint_before=None, checkpoint_after=None,
                now=None) -> PipelineJob:
    """Temporary failure: release ownership and put the job back on the queue for retry."""
    now = now or utcnow()
    job = _started_job(db, job_id, "requeue_job")
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?, scheduled_at=?, finished_at=NULL, started_at=NULL, outcome=NULL, "
                    "last_error_code=?, last_error_message=?, worker_id=NULL, claim_token=NULL, "
                    "lease_expires_at=NULL",
        effects_params=(JobStatus.QUEUED.value, now + timedelta(seconds=delay_seconds), error_code, error_message),
        attempt=_attempt_values(job, now, result_type="requeued", runner=runner, role=role,
                                error_code=error_code, error_message=error_message,
                                checkpoint_before=checkpoint_before, checkpoint_after=checkpoint_after),
        runner=runner,
    )


def move_to_waiting_capacity(db, job_id, worker_id, claim_token, *, runner=None, now=None) -> PipelineJob:
    """Processing -> waiting_capacity. NOT a business failure: attempts/outcome/error are untouched.

    Ownership is released so any capable runner may later pick the job up again.
    """
    now = now or utcnow()
    return _transition(
        db, job_id, worker_id, claim_token, now,
        effects_sql="status=?",
        effects_params=(JobStatus.WAITING_CAPACITY.value,),
        attempt=None,
        runner=runner,
    )


def promote_waiting_capacity(db, job_id, *, now=None) -> PipelineJob:
    """waiting_capacity -> queued so it becomes claimable again (scheduler hook, Phase 2+)."""
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

    Each guarded UPDATE re-checks status + lease in the WHERE clause, so a lease renewed
    between the scan and the update is skipped. Exhausted attempts fail terminally;
    otherwise the job is requeued with the attempts counter intact.
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
            row = tx.select_one("SELECT attempts, max_attempts, started_at FROM pipeline_jobs WHERE id=?", (job_id,))
            if row is None:
                continue
            attempts, max_attempts, started_at = row
            exhausted = attempts >= max_attempts
            cursor = tx.execute(
                "UPDATE pipeline_jobs SET status=?, finished_at=?, started_at=?, outcome=?, "
                "last_error_code=?, last_error_message=?, worker_id=NULL, claim_token=NULL, "
                "lease_expires_at=NULL, scheduled_at=?, updated_at=? "
                "WHERE id=? AND status=? AND lease_expires_at IS NOT NULL AND lease_expires_at<?",
                (
                    JobStatus.FAILED.value if exhausted else JobStatus.QUEUED.value,
                    now if exhausted else None,
                    None,
                    "failed" if exhausted else None,
                    "LEASE_EXPIRED",
                    "job lease expired; recovered by stale-job recovery",
                    now,
                    now,
                    job_id,
                    JobStatus.PROCESSING.value,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                continue  # a heartbeat renewed the lease since the scan
            recovered += 1
            tx.insert_each("runner_attempts", [{
                "id": uuid.uuid4().hex,
                "pipeline_job_id": job_id,
                "runner_instance_id": None,
                "role": "recovery",
                "attempt_number": attempts,
                "started_at": started_at,
                "finished_at": now,
                "result_type": "failed" if exhausted else "requeued",
                "checkpoint_before": None,
                "checkpoint_after": None,
                "error_code": "LEASE_EXPIRED",
                "error_message": "recovered after lease expiry",
            }])
    return recovered