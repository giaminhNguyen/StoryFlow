"""Database-level tests: fresh Alembic migration, SQLite pragmas."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.orm import sessionmaker

import storyflow.config
from storyflow.database import Base, make_engine
from storyflow.models import WorkflowSession

BACKEND = Path(__file__).resolve().parent.parent

def upgrade_to_head(url: str):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


@pytest.fixture
def migrated_db(monkeypatch, tmp_path):
    path = tmp_path / "migration_test.db"
    url = f"sqlite:///{path.as_posix()}"
    monkeypatch.setattr(storyflow.config.settings, "database_url", url)
    upgrade_to_head(url)
    yield make_engine(url)
    engine_maker = make_engine(url)
    engine_maker.dispose()


def test_fresh_migration_succeeds(migrated_db, tmp_path):
    insp = inspect(migrated_db)
    names = set(insp.get_table_names())
    assert {"workflow_sessions", "runner_instances", "pipeline_jobs", "runner_attempts",
            "channel_workflows", "story_projects", "source_snapshots", "canon_analyses",
            "story_generations", "story_versions", "tts_generations", "audio_generations",
            "audio_chunks"} <= names

    dedupe_index = next(i for i in insp.get_indexes("pipeline_jobs") if i["name"] == "ix_pipeline_jobs_active_dedupe")
    assert bool(dedupe_index["unique"]) is True
    assert "dedupe_key" in dedupe_index["column_names"]

    version_index = next(i for i in insp.get_indexes("story_versions")
                         if i["name"] == "uq_story_versions_active_number")
    assert bool(version_index["unique"]) is True
    assert version_index["column_names"] == ["story_project_id", "version_number"]

    Session = sessionmaker(bind=migrated_db, expire_on_commit=False)
    with Session() as db:
        session = WorkflowSession(mode="auto", status="active", all_agents_unavailable_policy="requeue")
        db.add(session)
        db.commit()
        loaded = db.get(WorkflowSession, session.id)
        assert loaded is not None and loaded.mode == "auto"


def test_sqlite_pragmas_wal_busy_timeout_foreign_keys(tmp_path):
    engine = make_engine(f"sqlite:///{(tmp_path / 'pragma.db').as_posix()}")
    with engine.connect() as conn:
        journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar()
        assert journal_mode == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == 5000
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1
    engine.dispose()