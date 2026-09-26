"""batch failure policy: source retry/backoff state and terminal project outcomes

Roadmap 4.2 / 4.6: a source that is temporarily blocked must be retried with backoff (not on every
tick) and one bad video must not stop the whole batch. Four additive columns on ``story_projects``:

* ``source_attempts`` / ``next_attempt_at``: consecutive transient source failures and the earliest
  time the next attempt may run (durable, so a restart never hammers the provider).
* ``status_reason`` / ``status_detail``: why a project ended as ``skipped`` / ``needs_attention``.

The new project statuses are plain string values in the existing ``status`` column: no schema change.

Revision ID: 0006_source_policy
Revises: 0005_control_plane
Create Date: 2026-09-26
"""

import sqlalchemy as sa

from alembic import op

revision = "0006_source_policy"
down_revision = "0005_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("story_projects", sa.Column("source_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("story_projects", sa.Column("next_attempt_at", sa.DateTime(), nullable=True))
    op.add_column("story_projects", sa.Column("status_reason", sa.String(64), nullable=True))
    op.add_column("story_projects", sa.Column("status_detail", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("story_projects") as batch:
        batch.drop_column("status_detail")
        batch.drop_column("status_reason")
        batch.drop_column("next_attempt_at")
        batch.drop_column("source_attempts")
