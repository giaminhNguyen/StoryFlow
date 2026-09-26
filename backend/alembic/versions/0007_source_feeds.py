"""multi-source ingestion: source feeds, per-project source settings, processed-video ledger

Roadmap 4.1: a workflow can hold many videos, coming from single links, playlists or whole channels,
and StoryFlow must know which video was already handled.

* ``source_feeds``: one row per expanded channel / playlist (title, newest-N limit, scan cursor).
* ``story_projects.video_id`` (+ index): the ledger key; existing projects are backfilled from the
  ``source.video_id`` of their workflow config so videos processed before this migration are known too.
* ``story_projects.source_config``: per-project source settings (override the workflow ``source`` block).
* ``story_projects.feed_id``: which feed a project came from (NULL for a directly added video).

All additive / nullable: existing rows stay valid.

Revision ID: 0007_source_feeds
Revises: 0006_source_policy
Create Date: 2026-09-26
"""

import json

import sqlalchemy as sa

from alembic import op

revision = "0007_source_feeds"
down_revision = "0006_source_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_feeds",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel_workflow_id", sa.String(36), sa.ForeignKey("channel_workflows.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("ref", sa.String(512), nullable=False),
        sa.Column("title", sa.String(255), nullable=True),
        sa.Column("limit_count", sa.Integer(), nullable=True),
        sa.Column("languages", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("known_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_scanned_at", sa.DateTime(), nullable=True),
        sa.Column("last_error", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_source_feeds_channel_workflow_id", "source_feeds", ["channel_workflow_id"])
    op.create_index("uq_source_feeds_ref", "source_feeds", ["channel_workflow_id", "kind", "ref"], unique=True)

    # Plain nullable columns (``feed_id`` is a soft reference, feeds are never deleted): SQLite cannot ALTER in a
    # foreign key, and recreating story_projects would fight the FKs of every table that points at it.
    op.add_column("story_projects", sa.Column("video_id", sa.String(64), nullable=True))
    op.add_column("story_projects", sa.Column("feed_id", sa.String(36), nullable=True))
    op.add_column("story_projects", sa.Column("source_config", sa.JSON(), nullable=True))
    op.create_index("ix_story_projects_video_id", "story_projects", ["video_id"])

    # Backfill the ledger key from the workflow-level source (single-video workflows made before 0007).
    con = op.get_bind()
    rows = con.execute(sa.text(
        "SELECT p.id, w.config FROM story_projects p JOIN channel_workflows w ON w.id = p.channel_workflow_id "
        "WHERE p.video_id IS NULL")).fetchall()
    for project_id, raw in rows:
        try:
            config = raw if isinstance(raw, dict) else json.loads(raw or "{}")
            video_id = ((config or {}).get("source") or {}).get("video_id")
        except (TypeError, ValueError):
            continue
        if isinstance(video_id, str) and 0 < len(video_id) <= 64:
            con.execute(sa.text("UPDATE story_projects SET video_id = :v WHERE id = :i"),
                        {"v": video_id, "i": project_id})


def downgrade() -> None:
    op.drop_index("ix_story_projects_video_id", table_name="story_projects")
    with op.batch_alter_table("story_projects", recreate="never") as batch:  # native DROP COLUMN, no table copy
        batch.drop_column("source_config")
        batch.drop_column("feed_id")
        batch.drop_column("video_id")
    op.drop_index("uq_source_feeds_ref", table_name="source_feeds")
    op.drop_index("ix_source_feeds_channel_workflow_id", table_name="source_feeds")
    op.drop_table("source_feeds")
