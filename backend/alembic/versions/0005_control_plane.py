"""control plane: durable workflow pause reason + stable runner identity

Phase 5 lifecycle needs two durable facts the Phase 3/4 schema could not hold:

* ``channel_workflows.status_reason`` / ``status_detail``: why a workflow is PAUSED
  (operator pause vs orchestrator-detected step failure, incl. inline source failures that
  leave no failed domain row). Without it, ``retry`` cannot be told apart from ``resume``
  after a restart. Additive nullable columns; existing rows stay valid (NULL).
* ``runner_instances.external_id`` + partial unique index on (runner_type, external_id):
  discovery upserts a runner by its provider identity so a restart rebuilds the in-memory
  registry from DB rows without granting any workflow session (workflow_session_id stays NULL
  until an explicit assignment command). Additive nullable column.

* ``channel_workflows.client_key`` + partial unique index: optional caller-supplied idempotency
  key so a retried "create workflow" (e.g. an HTTP retry) converges on one row, enforced by the
  database instead of an in-memory request cache. Additive nullable column.

New workflow statuses ``draft`` / ``cancelled`` are plain string values in the existing
String(32) status column: no schema change.

Revision ID: 0005_control_plane
Revises: 0004_pipeline_dedupe
Create Date: 2026-09-25
"""

import sqlalchemy as sa

from alembic import op

revision = "0005_control_plane"
down_revision = "0004_pipeline_dedupe"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("channel_workflows", sa.Column("status_reason", sa.String(64), nullable=True))
    op.add_column("channel_workflows", sa.Column("status_detail", sa.JSON(), nullable=True))
    op.add_column("channel_workflows", sa.Column("client_key", sa.String(128), nullable=True))
    op.create_index(
        "uq_channel_workflows_client_key", "channel_workflows", ["client_key"],
        unique=True, sqlite_where=sa.text("client_key IS NOT NULL"),
    )
    op.add_column("runner_instances", sa.Column("external_id", sa.String(128), nullable=True))
    op.create_index(
        "uq_runner_instances_external", "runner_instances", ["runner_type", "external_id"],
        unique=True, sqlite_where=sa.text("external_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_runner_instances_external", table_name="runner_instances")
    with op.batch_alter_table("runner_instances") as batch:
        batch.drop_column("external_id")
    op.drop_index("uq_channel_workflows_client_key", table_name="channel_workflows")
    with op.batch_alter_table("channel_workflows") as batch:
        batch.drop_column("client_key")
        batch.drop_column("status_detail")
        batch.drop_column("status_reason")
