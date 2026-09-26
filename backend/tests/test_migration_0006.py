"""Batch failure policy migration gate: 0006_source_policy is linear after 0005 and cycles cleanly."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import storyflow.config
from storyflow.database import make_engine

BACKEND = Path(__file__).resolve().parent.parent
NEW_COLUMNS = {"source_attempts", "next_attempt_at", "status_reason", "status_detail"}


def _cfg(url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _project_columns(url):
    engine = make_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("story_projects")}
    finally:
        engine.dispose()


def test_clean_cycle_and_existing_rows_keep_defaults(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'm6.db').as_posix()}"
    monkeypatch.setattr(storyflow.config.settings, "database_url", url)
    cfg = _cfg(url)

    command.upgrade(cfg, "0005_control_plane")
    assert not NEW_COLUMNS & _project_columns(url)
    engine = make_engine(url)
    with engine.begin() as con:
        con.exec_driver_sql(
            "INSERT INTO story_projects (id, title, status, created_at, updated_at) "
            "VALUES ('p1','t','active','2026-01-01 00:00:00.000000','2026-01-01 00:00:00.000000')")
    engine.dispose()

    command.upgrade(cfg, "head")
    assert NEW_COLUMNS <= _project_columns(url)
    engine = make_engine(url)
    with engine.connect() as con:  # an existing project starts with zero attempts and no reason
        assert con.exec_driver_sql(
            "SELECT status, source_attempts, next_attempt_at, status_reason, status_detail "
            "FROM story_projects WHERE id='p1'").one() == ("active", 0, None, None, None)
    engine.dispose()

    command.downgrade(cfg, "0005_control_plane")
    assert not NEW_COLUMNS & _project_columns(url)
    command.upgrade(cfg, "head")
    assert NEW_COLUMNS <= _project_columns(url)
