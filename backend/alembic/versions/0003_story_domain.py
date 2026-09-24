"""story domain: channel workflows, projects, snapshots, canon, versions, TTS/audio

Revision ID: 0003_story_domain
Revises: 0002_runner_dispatch
Create Date: 2026-09-25
"""

import sqlalchemy as sa

from alembic import op

revision = "0003_story_domain"
down_revision = "0002_runner_dispatch"
branch_labels = None
depends_on = None

# Partial unique dedupe targets (same pattern as ix_pipeline_jobs_active_dedupe).
SNAPSHOT_ACTIVE_WHERE = "status = 'active'"
VERSION_ACTIVE_WHERE = "status = 'active'"
CHUNK_ACTIVE_WHERE = "status = 'active'"
ANALYSIS_ACTIVE_WHERE = "status IN ('queued','processing')"
AUDIO_RUN_ACTIVE_WHERE = "status IN ('queued','processing')"


def upgrade() -> None:
    op.create_table(
        "channel_workflows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("workflow_session_id", sa.String(36), sa.ForeignKey("workflow_sessions.id"), nullable=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_channel_workflows_workflow_session_id", "channel_workflows", ["workflow_session_id"])
    op.create_table(
        "story_projects",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("channel_workflow_id", sa.String(36), sa.ForeignKey("channel_workflows.id"), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(128), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("uq_story_projects_slug", "story_projects", ["slug"], unique=True)
    op.create_index("ix_story_projects_channel_workflow_id", "story_projects", ["channel_workflow_id"])
    op.create_table(
        "source_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("story_project_id", sa.String(36), sa.ForeignKey("story_projects.id"), nullable=False),
        sa.Column("snapshot_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_source_snapshots_story_project_id", "source_snapshots", ["story_project_id"])
    op.create_index(
        "uq_source_snapshots_active_number", "source_snapshots",
        ["story_project_id", "snapshot_number"], unique=True, sqlite_where=sa.text(SNAPSHOT_ACTIVE_WHERE),
    )
    op.create_table(
        "canon_analyses",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_snapshot_id", sa.String(36), sa.ForeignKey("source_snapshots.id"), nullable=False),
        sa.Column("pipeline_job_id", sa.String(36), sa.ForeignKey("pipeline_jobs.id"), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("canon", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_canon_analyses_source_snapshot_id", "canon_analyses", ["source_snapshot_id"])
    op.create_index(
        "uq_canon_analyses_active", "canon_analyses", ["source_snapshot_id"],
        unique=True, sqlite_where=sa.text(ANALYSIS_ACTIVE_WHERE),
    )
    op.create_table(
        "story_generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("story_project_id", sa.String(36), sa.ForeignKey("story_projects.id"), nullable=False),
        sa.Column("source_snapshot_id", sa.String(36), sa.ForeignKey("source_snapshots.id"), nullable=True),
        sa.Column("canon_analysis_id", sa.String(36), sa.ForeignKey("canon_analyses.id"), nullable=True),
        sa.Column("pipeline_job_id", sa.String(36), sa.ForeignKey("pipeline_jobs.id"), nullable=True),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_story_generations_story_project_id", "story_generations", ["story_project_id"])
    op.create_index("ix_story_generations_status", "story_generations", ["status"])
    op.create_table(
        "story_versions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("story_generation_id", sa.String(36), sa.ForeignKey("story_generations.id"), nullable=True),
        sa.Column("story_project_id", sa.String(36), sa.ForeignKey("story_projects.id"), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("word_count", sa.Integer(), nullable=False),
        sa.Column("content_path", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_story_versions_story_generation_id", "story_versions", ["story_generation_id"])
    op.create_index("ix_story_versions_story_project_id", "story_versions", ["story_project_id"])
    op.create_index(
        "uq_story_versions_active_number", "story_versions",
        ["story_project_id", "version_number"], unique=True, sqlite_where=sa.text(VERSION_ACTIVE_WHERE),
    )
    op.create_table(
        "tts_generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("story_version_id", sa.String(36), sa.ForeignKey("story_versions.id"), nullable=False),
        sa.Column("pipeline_job_id", sa.String(36), sa.ForeignKey("pipeline_jobs.id"), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("voice", sa.String(64), nullable=False),
        sa.Column("engine", sa.String(64), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_tts_generations_story_version_id", "tts_generations", ["story_version_id"])
    op.create_table(
        "audio_generations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tts_generation_id", sa.String(36), sa.ForeignKey("tts_generations.id"), nullable=False),
        sa.Column("run_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("chunk_count", sa.Integer(), nullable=False),
        sa.Column("store_dir", sa.String(512), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_audio_generations_tts_generation_id", "audio_generations", ["tts_generation_id"])
    op.create_index(
        "uq_audio_generations_active_run", "audio_generations",
        ["tts_generation_id", "run_number"], unique=True, sqlite_where=sa.text(AUDIO_RUN_ACTIVE_WHERE),
    )
    op.create_table(
        "audio_chunks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("audio_generation_id", sa.String(36), sa.ForeignKey("audio_generations.id"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("artifact_path", sa.String(512), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_audio_chunks_audio_generation_id", "audio_chunks", ["audio_generation_id"])
    op.create_index(
        "uq_audio_chunks_active_index", "audio_chunks",
        ["audio_generation_id", "chunk_index"], unique=True, sqlite_where=sa.text(CHUNK_ACTIVE_WHERE),
    )


def downgrade() -> None:
    op.drop_index("uq_audio_chunks_active_index", table_name="audio_chunks")
    op.drop_index("ix_audio_chunks_audio_generation_id", table_name="audio_chunks")
    op.drop_table("audio_chunks")
    op.drop_index("uq_audio_generations_active_run", table_name="audio_generations")
    op.drop_index("ix_audio_generations_tts_generation_id", table_name="audio_generations")
    op.drop_table("audio_generations")
    op.drop_index("ix_tts_generations_story_version_id", table_name="tts_generations")
    op.drop_table("tts_generations")
    op.drop_index("uq_story_versions_active_number", table_name="story_versions")
    op.drop_index("ix_story_versions_story_project_id", table_name="story_versions")
    op.drop_index("ix_story_versions_story_generation_id", table_name="story_versions")
    op.drop_table("story_versions")
    op.drop_index("ix_story_generations_status", table_name="story_generations")
    op.drop_index("ix_story_generations_story_project_id", table_name="story_generations")
    op.drop_table("story_generations")
    op.drop_index("uq_canon_analyses_active", table_name="canon_analyses")
    op.drop_index("ix_canon_analyses_source_snapshot_id", table_name="canon_analyses")
    op.drop_table("canon_analyses")
    op.drop_index("uq_source_snapshots_active_number", table_name="source_snapshots")
    op.drop_index("ix_source_snapshots_story_project_id", table_name="source_snapshots")
    op.drop_table("source_snapshots")
    op.drop_index("ix_story_projects_channel_workflow_id", table_name="story_projects")
    op.drop_index("uq_story_projects_slug", table_name="story_projects")
    op.drop_table("story_projects")
    op.drop_index("ix_channel_workflows_workflow_session_id", table_name="channel_workflows")
    op.drop_table("channel_workflows")