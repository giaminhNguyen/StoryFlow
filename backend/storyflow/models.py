"""StoryFlow Phase 1 data model: sessions, runner instances, jobs, runner attempts.

No future Story/StoryVersion/TTS/Audio models yet.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base
from .roles import DEFAULT_ROLE, DEFAULT_SUPPORTED_ROLES


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
    all_agents_unavailable_policy: Mapped[str] = mapped_column(String(32), default="pause_auto_resume")
    # Optional ordered runner_type preference per role: {role: [runner_type, ...]}.
    role_preferences: Mapped[dict] = mapped_column(JSON, default=dict)
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
    # Roles this runner can serve (role = work type, runner_type = process kind).
    supported_roles: Mapped[list] = mapped_column(JSON, default=list(DEFAULT_SUPPORTED_ROLES))
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    quota_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class PipelineJob(Base):
    __tablename__ = "pipeline_jobs"
    __table_args__ = (
        Index("ix_pipeline_jobs_claim", "status", "priority", "scheduled_at", "created_at"),
        Index("ix_pipeline_jobs_status_lease", "status", "lease_expires_at"),
        Index("ix_pipeline_jobs_workflow_session_id", "workflow_session_id"),
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
    role: Mapped[str] = mapped_column(String(64), default=DEFAULT_ROLE)
    # The job's allow-list: only runners of this session may touch it.
    workflow_session_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_sessions.id"), nullable=True)
    # Number of real dispatches (infra + business combined); purely observational.
    execution_count: Mapped[int] = mapped_column(Integer, default=0)
    # Business attempt counter: incremented ONLY on business failures (task_failed,
    # invalid_output). -- NOT on quota/rate-limit/unavailability/waiting.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    # Infrastructure failures (crash/timeout/stale lease). Guards against crash
    # feedback loops without touching the business `attempts` counter.
    infrastructure_failures: Mapped[int] = mapped_column(Integer, default=0)
    max_infra_attempts: Mapped[int] = mapped_column(Integer, default=5)
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


# --- Phase 3: Story domain ---------------------------------------------------
# Story pipeline: channel workflows drive projects; snapshots capture source story
# inputs; canon analyses turn a snapshot into canon facts; generations turn canon +
# source into StoryVersions; TTS generations turn a version into audio runs/chunks.
# Job rows (phase 2) are infra; `pipeline_job_id` here is a nullable trace link only.


class DomainStatus(str, enum.Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ChannelWorkflowStatus(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    FINISHED = "finished"
    ABANDONED = "abandoned"


class VersionStatus(str, enum.Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"


# Partial unique dedupe targets, shared by the model indexes and the migration
# (same pattern as ACTIVE_DEDUPE_WHERE on pipeline_jobs).
SNAPSHOT_ACTIVE_WHERE = "status = 'active'"
VERSION_ACTIVE_WHERE = "status = 'active'"
CHUNK_ACTIVE_WHERE = "status = 'active'"
ANALYSIS_ACTIVE_WHERE = "status IN ('queued','processing')"
AUDIO_RUN_ACTIVE_WHERE = "status IN ('queued','processing')"


class ChannelWorkflow(Base):
    __tablename__ = "channel_workflows"
    __table_args__ = (Index("ix_channel_workflows_workflow_session_id", "workflow_session_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    workflow_session_id: Mapped[str | None] = mapped_column(ForeignKey("workflow_sessions.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default=ChannelWorkflowStatus.ACTIVE.value)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class StoryProject(Base):
    __tablename__ = "story_projects"
    __table_args__ = (
        Index("uq_story_projects_slug", "slug", unique=True),
        Index("ix_story_projects_channel_workflow_id", "channel_workflow_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    channel_workflow_id: Mapped[str | None] = mapped_column(ForeignKey("channel_workflows.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str | None] = mapped_column(String(128), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=VersionStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (
        Index("ix_source_snapshots_story_project_id", "story_project_id"),
        Index("uq_source_snapshots_active_number", "story_project_id", "snapshot_number",
              unique=True, sqlite_where=text(SNAPSHOT_ACTIVE_WHERE)),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    story_project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.id"))
    snapshot_number: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default=VersionStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CanonAnalysis(Base):
    __tablename__ = "canon_analyses"
    __table_args__ = (
        Index("ix_canon_analyses_source_snapshot_id", "source_snapshot_id"),
        Index("uq_canon_analyses_active", "source_snapshot_id", unique=True,
              sqlite_where=text(ANALYSIS_ACTIVE_WHERE)),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_snapshot_id: Mapped[str] = mapped_column(ForeignKey("source_snapshots.id"))
    pipeline_job_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_jobs.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=DomainStatus.QUEUED.value)
    canon: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class StoryGeneration(Base):
    __tablename__ = "story_generations"
    __table_args__ = (
        Index("ix_story_generations_story_project_id", "story_project_id"),
        Index("ix_story_generations_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    story_project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.id"))
    source_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("source_snapshots.id"), nullable=True)
    canon_analysis_id: Mapped[str | None] = mapped_column(ForeignKey("canon_analyses.id"), nullable=True)
    pipeline_job_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_jobs.id"), nullable=True)
    trigger: Mapped[str] = mapped_column(String(32), default="manual")
    status: Mapped[str] = mapped_column(String(32), default=DomainStatus.QUEUED.value)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class StoryVersion(Base):
    __tablename__ = "story_versions"
    __table_args__ = (
        Index("ix_story_versions_story_generation_id", "story_generation_id"),
        Index("ix_story_versions_story_project_id", "story_project_id"),
        Index("uq_story_versions_active_number", "story_project_id", "version_number",
              unique=True, sqlite_where=text(VERSION_ACTIVE_WHERE)),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    story_generation_id: Mapped[str | None] = mapped_column(ForeignKey("story_generations.id"), nullable=True)
    story_project_id: Mapped[str] = mapped_column(ForeignKey("story_projects.id"))
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer, default=0)
    # Relative artifact path under the artifact store root (see storyflow/artifacts.py).
    content_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=VersionStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TTSGeneration(Base):
    __tablename__ = "tts_generations"
    __table_args__ = (Index("ix_tts_generations_story_version_id", "story_version_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    story_version_id: Mapped[str] = mapped_column(ForeignKey("story_versions.id"))
    pipeline_job_id: Mapped[str | None] = mapped_column(ForeignKey("pipeline_jobs.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=DomainStatus.QUEUED.value)
    voice: Mapped[str] = mapped_column(String(64))
    engine: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AudioGeneration(Base):
    __tablename__ = "audio_generations"
    __table_args__ = (
        Index("ix_audio_generations_tts_generation_id", "tts_generation_id"),
        Index("uq_audio_generations_active_run", "tts_generation_id", "run_number",
              unique=True, sqlite_where=text(AUDIO_RUN_ACTIVE_WHERE)),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    tts_generation_id: Mapped[str] = mapped_column(ForeignKey("tts_generations.id"))
    run_number: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default=DomainStatus.QUEUED.value)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    # Relative directory under the artifact store root holding this run's chunks.
    store_dir: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AudioChunk(Base):
    __tablename__ = "audio_chunks"
    __table_args__ = (
        Index("ix_audio_chunks_audio_generation_id", "audio_generation_id"),
        Index("uq_audio_chunks_active_index", "audio_generation_id", "chunk_index",
              unique=True, sqlite_where=text(CHUNK_ACTIVE_WHERE)),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    audio_generation_id: Mapped[str] = mapped_column(ForeignKey("audio_generations.id"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    # Relative artifact path under the artifact store root.
    artifact_path: Mapped[str] = mapped_column(String(512))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default=VersionStatus.ACTIVE.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)