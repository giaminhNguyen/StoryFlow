"""Typed contracts between the dispatcher and its runners.

TaskPacket deliberately carries NO database credentials, claim tokens, lease info or
SQLite connection state — agents receive facts needed to do the work, and report results
through RunnerResult. Agents never touch the DB directly (the dispatcher owns all
persistence).
"""

import enum
from dataclasses import dataclass, field
from datetime import datetime


class ResultCode(str, enum.Enum):
    SUCCESS = "success"
    TASK_FAILED = "task_failed"
    INVALID_OUTPUT = "invalid_output"
    TRANSIENT_FAILURE = "transient_failure"
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    AUTH_ERROR = "auth_error"
    RUNNER_CRASHED = "runner_crashed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


# Business failures: count against PipelineJob.attempts (retried up to max_attempts).
BUSINESS_FAILURE_CODES = (ResultCode.TASK_FAILED, ResultCode.INVALID_OUTPUT)
# Infrastructure failures: count against PipelineJob.infrastructure_failures.
INFRASTRUCTURE_FAILURE_CODES = (
    ResultCode.TRANSIENT_FAILURE,
    ResultCode.RATE_LIMITED,
    ResultCode.QUOTA_EXHAUSTED,
    ResultCode.AUTH_ERROR,
    ResultCode.RUNNER_CRASHED,
    ResultCode.TIMEOUT,
)


@dataclass(frozen=True)
class RunnerResult:
    code: ResultCode
    artifacts: dict | None = None
    checkpoint_before: dict | None = None
    checkpoint_after: dict | None = None
    checkpoint_metadata: dict | None = None
    retry_after: float | None = None          # seconds, when rate-limited
    quota_reset_at: datetime | None = None     # when quota resets
    next_run_at: datetime | None = None        # explicit "do not run before" hint
    metrics: dict | None = None
    error_code: str | None = None
    error_message: str | None = None           # sanitized: no secrets, bounded length


@dataclass(frozen=True)
class TaskPacket:
    protocol_version: str = "1"
    task_id: str = field(default="")
    job_id: str = field(default="")
    role: str = field(default="")
    skill_name: str | None = None
    skill_path: str | None = None
    skill_revision: str | None = None
    workspace_root: str | None = None
    write_scope: str = "workspace"
    inputs: dict = field(default_factory=dict)
    outputs: list = field(default_factory=list)
    task_config: dict = field(default_factory=dict)
    constraints: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RunnerHealth:
    ok: bool
    runner_type: str
    state: str | None = None
    error_code: str | None = None
    error_message: str | None = None