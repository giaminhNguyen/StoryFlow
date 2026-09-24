"""initial schema: workflow sessions, runner instances, pipeline jobs, runner attempts

Revision ID: 0001_storyflow_initial
Revises:
Create Date: 2026-09-25
"""

import sqlalchemy as sa

from alembic import op

revision = "0001_storyflow_initial"
down_revision = None
branch_labels = None
depends_on = None

ACTIVE_DEDUPE_WHERE = "dedupe_key IS NOT NULL AND status IN ('queued','processing','waiting_capacity')"


def upgrade() -> None:
    op.create_table(
        "workflow_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("mode", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("all_agents_unavailable_policy", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "runner_instances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workflow_session_id", sa.String(36), sa.ForeignKey("workflow_sessions.id"), nullable=True),
        sa.Column("runner_type", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("max_concurrency", sa.Integer(), nullable=False),
        sa.Column("active_count", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(), nullable=True),
        sa.Column("quota_reset_at", sa.DateTime(), nullable=True),
        sa.Column("last_health_at", sa.DateTime(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_runner_instances_workflow_session_id", "runner_instances", ["workflow_session_id"])
    op.create_table(
        "pipeline_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("channel_fairness_key", sa.String(128), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("worker_id", sa.String(128), nullable=True),
        sa.Column("claim_token", sa.String(32), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("dedupe_key", sa.String(255), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_pipeline_jobs_claim", "pipeline_jobs", ["status", "priority", "scheduled_at", "created_at"])
    op.create_index("ix_pipeline_jobs_status_lease", "pipeline_jobs", ["status", "lease_expires_at"])
    op.create_index(
        "ix_pipeline_jobs_active_dedupe",
        "pipeline_jobs",
        ["dedupe_key"],
        unique=True,
        sqlite_where=sa.text(ACTIVE_DEDUPE_WHERE),
    )
    op.create_table(
        "runner_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("pipeline_job_id", sa.String(36), sa.ForeignKey("pipeline_jobs.id"), nullable=False),
        sa.Column("runner_instance_id", sa.String(36), sa.ForeignKey("runner_instances.id"), nullable=True),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("result_type", sa.String(32), nullable=True),
        sa.Column("checkpoint_before", sa.JSON(), nullable=True),
        sa.Column("checkpoint_after", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
    )
    op.create_index("ix_runner_attempts_pipeline_job_id", "runner_attempts", ["pipeline_job_id"])
    op.create_index("ix_runner_attempts_runner_instance_id", "runner_attempts", ["runner_instance_id"])


def downgrade() -> None:
    op.drop_index("ix_runner_attempts_runner_instance_id", table_name="runner_attempts")
    op.drop_index("ix_runner_attempts_pipeline_job_id", table_name="runner_attempts")
    op.drop_table("runner_attempts")
    op.drop_index("ix_pipeline_jobs_active_dedupe", table_name="pipeline_jobs")
    op.drop_index("ix_pipeline_jobs_status_lease", table_name="pipeline_jobs")
    op.drop_index("ix_pipeline_jobs_claim", table_name="pipeline_jobs")
    op.drop_table("pipeline_jobs")
    op.drop_index("ix_runner_instances_workflow_session_id", table_name="runner_instances")
    op.drop_table("runner_instances")
    op.drop_table("workflow_sessions")