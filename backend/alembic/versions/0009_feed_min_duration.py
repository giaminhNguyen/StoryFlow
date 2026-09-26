"""source feeds remember their minimum video duration

A channel added with ``min_duration_seconds`` must keep filtering short videos on every re-scan (``sync``);
without the value on the feed, a sync silently re-added everything the first add had filtered out.
One additive nullable column; existing feeds keep NULL = no duration filter.

Revision ID: 0009_feed_min_duration
Revises: 0008_story_reviews
Create Date: 2026-09-26
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_feed_min_duration"
down_revision = "0008_story_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("source_feeds")}
    if "min_duration_seconds" not in columns:
        op.add_column("source_feeds", sa.Column("min_duration_seconds", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("source_feeds", recreate="never") as batch:   # native DROP COLUMN, no table copy
        batch.drop_column("min_duration_seconds")
