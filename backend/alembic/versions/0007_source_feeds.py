"""multi-source ingestion: source feeds, per-project source settings, processed-video ledger

Roadmap 4.1: a workflow can hold many videos, coming from single links, playlists or whole channels,
and StoryFlow must know which video was already handled.

* ``source_feeds``: one row per expanded channel / playlist (title, first-scan limit, scan cursor).
* ``story_projects.video_id`` (+ index): the ledger key; existing projects are backfilled from the
  ``source.video_id`` of their workflow config so videos processed before this migration are known too.
* ``story_projects.source_config``: per-project source settings (override the workflow ``source`` block).
* ``story_projects.feed_id``: which feed a project came from (NULL for a directly added video).

All additive / nullable: existing rows stay valid. Every step checks what already exists, so re-running the
upgrade after a failed (half-applied) attempt is safe; the backfill skips any config that is not the expected
``{"source": {"video_id": "..."}}`` shape instead of crashing.

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


def _existing(table: str) -> tuple[set, set]:
    """(column names, index names) of ``table`` right now; empty when the table does not exist. Lets every step
    below be re-run safely (SQLite does not roll DDL back, so a failed upgrade can leave it half applied)."""
    insp = sa.inspect(op.get_bind())
    if table not in insp.get_table_names():
        return set(), set()
    return {c["name"] for c in insp.get_columns(table)}, {i["name"] for i in insp.get_indexes(table)}


def _create_source_feeds() -> None:
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


def _video_id_of(raw):
    """The workflow-level ``source.video_id`` of a stored config, or None for any other shape."""
    try:
        config = raw if isinstance(raw, dict) else json.loads(raw or "null")
    except (TypeError, ValueError):
        return None
    source = config.get("source") if isinstance(config, dict) else None
    video_id = source.get("video_id") if isinstance(source, dict) else None
    return video_id if isinstance(video_id, str) and 0 < len(video_id) <= 64 else None


def _backfill_ledger() -> None:
    con = op.get_bind()
    rows = con.execute(sa.text(
        "SELECT p.id, w.config FROM story_projects p JOIN channel_workflows w ON w.id = p.channel_workflow_id "
        "WHERE p.video_id IS NULL")).fetchall()
    for project_id, raw in rows:
        video_id = _video_id_of(raw)
        if video_id is not None:
            con.execute(sa.text("UPDATE story_projects SET video_id = :v WHERE id = :i"),
                        {"v": video_id, "i": project_id})


def upgrade() -> None:
    feed_columns, feed_indexes = _existing("source_feeds")
    if not feed_columns:
        _create_source_feeds()
        feed_indexes = set()
    if "ix_source_feeds_channel_workflow_id" not in feed_indexes:
        op.create_index("ix_source_feeds_channel_workflow_id", "source_feeds", ["channel_workflow_id"])
    if "uq_source_feeds_ref" not in feed_indexes:
        op.create_index("uq_source_feeds_ref", "source_feeds", ["channel_workflow_id", "kind", "ref"], unique=True)

    project_columns, project_indexes = _existing("story_projects")
    # Plain nullable columns (``feed_id`` is a soft reference, feeds are never deleted): SQLite cannot ALTER in a
    # foreign key, and recreating story_projects would fight the FKs of every table that points at it.
    if "video_id" not in project_columns:
        op.add_column("story_projects", sa.Column("video_id", sa.String(64), nullable=True))
    if "feed_id" not in project_columns:
        op.add_column("story_projects", sa.Column("feed_id", sa.String(36), nullable=True))
    if "source_config" not in project_columns:
        op.add_column("story_projects", sa.Column("source_config", sa.JSON(), nullable=True))
    if "ix_story_projects_video_id" not in project_indexes:
        op.create_index("ix_story_projects_video_id", "story_projects", ["video_id"])
    _backfill_ledger()


def downgrade() -> None:
    op.drop_index("ix_story_projects_video_id", table_name="story_projects")
    with op.batch_alter_table("story_projects", recreate="never") as batch:  # native DROP COLUMN, no table copy
        batch.drop_column("source_config")
        batch.drop_column("feed_id")
        batch.drop_column("video_id")
    op.drop_index("uq_source_feeds_ref", table_name="source_feeds")
    op.drop_index("ix_source_feeds_channel_workflow_id", table_name="source_feeds")
    op.drop_table("source_feeds")
