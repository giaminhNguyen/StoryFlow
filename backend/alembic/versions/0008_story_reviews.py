"""story review + revision (roadmap 4.4): one review round per story version

``story_reviews`` records each review round: the verdict (approve / revise), the issues found and, when the
reviewer also produced a revised story, the newer ``story_versions`` row (``revised_version_id`` is a soft
reference). A partial unique index allows one live (queued / processing / completed) review per story version.

Purely additive: a workflow without a ``review`` block never creates a row.

Revision ID: 0008_story_reviews
Revises: 0007_source_feeds
Create Date: 2026-09-26
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_story_reviews"
down_revision = "0007_source_feeds"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "story_reviews",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("story_project_id", sa.String(36), sa.ForeignKey("story_projects.id"), nullable=False),
        sa.Column("story_version_id", sa.String(36), sa.ForeignKey("story_versions.id"), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("pipeline_job_id", sa.String(36), sa.ForeignKey("pipeline_jobs.id"), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("findings", sa.JSON(), nullable=True),
        sa.Column("revised_version_id", sa.String(36), nullable=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_story_reviews_story_project_id", "story_reviews", ["story_project_id"])
    op.create_index(
        "uq_story_reviews_live_input", "story_reviews", ["story_version_id"], unique=True,
        sqlite_where=sa.text("status IN ('queued','processing','completed')"),
    )


def downgrade() -> None:
    op.drop_index("uq_story_reviews_live_input", table_name="story_reviews")
    op.drop_index("ix_story_reviews_story_project_id", table_name="story_reviews")
    op.drop_table("story_reviews")
