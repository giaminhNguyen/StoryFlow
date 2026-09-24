"""pipeline orchestration: one live story/TTS generation per input

Phase 4 schedules StoryGeneration/TTSGeneration rows idempotently and concurrently.
Unlike canon_analyses/audio_generations (Phase 3), these two tables had no schema-level
guard, so two schedulers racing could each insert a live row for the same input.
Index only, no column changes. Failed/cancelled rows are outside the partial predicate,
so an explicit retry can create a fresh row.

Revision ID: 0004_pipeline_dedupe
Revises: 0003_story_domain
Create Date: 2026-09-25
"""

import sqlalchemy as sa

from alembic import op

revision = "0004_pipeline_dedupe"
down_revision = "0003_story_domain"
branch_labels = None
depends_on = None

GENERATION_LIVE_WHERE = "status IN ('queued','processing','completed')"


def upgrade() -> None:
    op.create_index(
        "uq_story_generations_live_input", "story_generations",
        ["story_project_id", "source_snapshot_id", "canon_analysis_id"],
        unique=True, sqlite_where=sa.text(GENERATION_LIVE_WHERE),
    )
    op.create_index(
        "uq_tts_generations_live_input", "tts_generations",
        ["story_version_id", "voice", "engine"],
        unique=True, sqlite_where=sa.text(GENERATION_LIVE_WHERE),
    )


def downgrade() -> None:
    op.drop_index("uq_tts_generations_live_input", table_name="tts_generations")
    op.drop_index("uq_story_generations_live_input", table_name="story_generations")
