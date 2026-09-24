"""runner dispatch: session allow-list, roles, counters, attempt lifecycle

Revision ID: 0002_runner_dispatch
Revises: 0001_storyflow_initial
Create Date: 2026-09-25
"""

import sqlalchemy as sa

from alembic import op

revision = "0002_runner_dispatch"
down_revision = "0001_storyflow_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # WorkflowSession: optional per-role runner_type preference
    op.add_column("workflow_sessions", sa.Column("role_preferences", sa.JSON(), nullable=False, server_default="{}"))
    # RunnerInstance: role support + LRU hint
    op.add_column(
        "runner_instances", sa.Column("supported_roles", sa.JSON(), nullable=False,
                                      server_default='["general_worker"]')
    )
    op.add_column("runner_instances", sa.Column("last_used_at", sa.DateTime(), nullable=True))
    # PipelineJob: allow-list link, role, infra/business counters
    op.add_column("pipeline_jobs", sa.Column("role", sa.String(64), nullable=False, server_default="general_worker"))
    # FK omitted at the DB level: SQLite cannot ALTER-ADD a constraint; the ORM
    # relationship in models.py still carries it. Session link is via app logic.
    op.add_column(
        "pipeline_jobs",
        sa.Column("workflow_session_id", sa.String(36), nullable=True),
    )
    op.add_column("pipeline_jobs", sa.Column("execution_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("pipeline_jobs", sa.Column("infrastructure_failures", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("pipeline_jobs", sa.Column("max_infra_attempts", sa.Integer(), nullable=False, server_default="5"))
    op.create_index("ix_pipeline_jobs_workflow_session_id", "pipeline_jobs", ["workflow_session_id"])


def downgrade() -> None:
    op.drop_index("ix_pipeline_jobs_workflow_session_id", table_name="pipeline_jobs")
    op.drop_column("pipeline_jobs", "max_infra_attempts")
    op.drop_column("pipeline_jobs", "infrastructure_failures")
    op.drop_column("pipeline_jobs", "execution_count")
    op.drop_column("pipeline_jobs", "workflow_session_id")
    op.drop_column("pipeline_jobs", "role")
    op.drop_column("runner_instances", "last_used_at")
    op.drop_column("runner_instances", "supported_roles")
    op.drop_column("workflow_sessions", "role_preferences")