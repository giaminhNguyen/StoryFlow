"""Feed duration filter migration gate: 0009_feed_min_duration is linear after 0008 and cycles cleanly."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import storyflow.config
from storyflow.database import make_engine

BACKEND = Path(__file__).resolve().parent.parent
TS = "'2026-01-01 00:00:00.000000'"


def _cfg(url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _feed_columns(url):
    engine = make_engine(url)
    try:
        return {c["name"] for c in inspect(engine).get_columns("source_feeds")}
    finally:
        engine.dispose()


def test_clean_cycle_existing_feeds_keep_no_filter(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'm9.db').as_posix()}"
    monkeypatch.setattr(storyflow.config.settings, "database_url", url)
    cfg = _cfg(url)

    command.upgrade(cfg, "0008_story_reviews")
    assert "min_duration_seconds" not in _feed_columns(url)
    engine = make_engine(url)
    with engine.begin() as con:
        con.exec_driver_sql(
            f"INSERT INTO channel_workflows (id, name, mode, status, config, created_at, updated_at) "
            f"VALUES ('w','n','auto','active','{{}}',{TS},{TS})")
        con.exec_driver_sql(
            f"INSERT INTO source_feeds (id, channel_workflow_id, kind, ref, limit_count, status, known_count, "
            f"created_at, updated_at) VALUES ('f','w','channel','https://www.youtube.com/@x',10,'active',3,{TS},{TS})")
    engine.dispose()

    command.upgrade(cfg, "head")
    assert "min_duration_seconds" in _feed_columns(url)
    engine = make_engine(url)
    with engine.connect() as con:      # an existing feed keeps working: NULL = no duration filter
        assert con.exec_driver_sql(
            "SELECT limit_count, min_duration_seconds FROM source_feeds WHERE id='f'").one() == (10, None)
    engine.dispose()

    command.downgrade(cfg, "0008_story_reviews")
    assert "min_duration_seconds" not in _feed_columns(url)
    engine = make_engine(url)
    with engine.connect() as con:      # the downgrade kept the rest of the row
        assert con.exec_driver_sql("SELECT limit_count FROM source_feeds WHERE id='f'").scalar() == 10
    engine.dispose()
    command.upgrade(cfg, "head")
    assert "min_duration_seconds" in _feed_columns(url)


def test_upgrade_is_safe_when_the_column_already_exists(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'm9b.db').as_posix()}"
    monkeypatch.setattr(storyflow.config.settings, "database_url", url)
    cfg = _cfg(url)
    command.upgrade(cfg, "0008_story_reviews")
    engine = make_engine(url)
    with engine.begin() as con:        # a half-applied earlier attempt
        con.exec_driver_sql("ALTER TABLE source_feeds ADD COLUMN min_duration_seconds INTEGER")
    engine.dispose()
    command.upgrade(cfg, "head")
    assert "min_duration_seconds" in _feed_columns(url)
