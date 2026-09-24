"""StoryFlow Phase 1 data model: sessions, runner instances, jobs, runner attempts.

No future Story/StoryVersion/TTS/Audio models yet.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def uid() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    """Naive UTC timestamp, stored/compared consistently by SQLite's DateTime adapter."""
    return datetime.now().astimezone().replace(tzinfo=None)


class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_CAPACITY = "waiting_capacity"


# A job with one of these statuses is "active": it occupies the logical slot for its dedupe_key.
ACTIVE_STATUSES = (JobStatus.QUEUED, JobStatus.PROCESSING, JobStatus.WAITING_CAPACITY)
ACTIVE_STATUS_VALUES = tuple(s.value for s in ACTIVE_STATUSES)

# Partial unique dedupe target, shared by the model index, the migration, and enqueue's ON CONFLICT.
ACTIVE_DEDUPE_WHERE = "dedupe_key IS NOT NULL AND status IN ('queued','processing','waiting_capacity')"


class RunnerState(str, enum.Enum):
    READY = "ready"
    BUSY = "busy"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    COOLDOWN = "cooldown"
    AUTH_ERROR = "auth_error"
    OFFLINE = "offline"
    DISABLED = "disabled"


class SessionStatus(str, enum.Enum):
    ACTIVE = "active"
    FINISHED = "finished"
    ABANDONED = "abandoned"


class WorkflowSession(Base):
    __tablename__ = "workflow_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    mode: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default=SessionStatus.ACTIVE.value)
    all_agents_unavailable_policy: Mapped[str] = mapped_column(String(32), default="requeue")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RunnerInstance(Base):
    __tablename__ = "runner_instances"
    __table_args__ = (Index("ix_runner_instances_workflow_session_id", "workflow_session_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    # Sessions are created in later phases; a runner may outlive/succeed a session, hence nullable.
    workflow_session_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_sessions.id"), nullable=True)
    runner_type: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_concurrency: Mapped[int] = mapped_column(Integer, default=1)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(32), default=RunnerState.READY.value)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quota_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PipelineJob(Base):
    __tablename__ = "pipeline_jobs"
    __table_args__ = (
        Index("ix_pipeline_jobs_claim", "status", "priority", "scheduled_at", "created_at"),
        Index("ix_pipeline_jobs_status_lease", "status", "lease_expires_at"),
        Index(
            "ix_pipeline_jobs_active_dedupe",
            "dedupe_key",
            unique=True,
            sqlite_where=text(ACTIVE_DEDUPE_WHERE),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default=JobStatus.QUEUED.value)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    channel_fairness_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    dedupe_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RunnerAttempt(Base):
    __tablename__ = "runner_attempts"
    __table_args__ = (
        Index("ix_runner_attempts_pipeline_job_id", "pipeline_job_id"),
        Index("ix_runner_attempts_runner_instance_id", "runner_instance_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    pipeline_job_id: Mapped[str] = mapped_column(ForeignKey("pipeline_jobs.id"))
    runner_instance_id: Mapped[str | None] = mapped_column(ForeignKey("runner_instances.id"), nullable=True)
    role: Mapped[str] = mapped_column(String(64))
    attempt_number: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    result_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    checkpoint_before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    checkpoint_after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)