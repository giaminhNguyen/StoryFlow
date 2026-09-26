"""Multi-source migration gate: 0007_source_feeds is linear after 0006, backfills the ledger and cycles cleanly."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import storyflow.config
from storyflow.database import make_engine

BACKEND = Path(__file__).resolve().parent.parent
NEW_COLUMNS = {"video_id", "feed_id", "source_config"}
TS = "'2026-01-01 00:00:00.000000'"


def _cfg(url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _shape(url):
    engine = make_engine(url)
    try:
        insp = inspect(engine)
        return ({c["name"] for c in insp.get_columns("story_projects")}, set(insp.get_table_names()),
                {i["name"] for i in insp.get_indexes("story_projects")})
    finally:
        engine.dispose()


def test_clean_cycle_and_ledger_backfill(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'm7.db').as_posix()}"
    monkeypatch.setattr(storyflow.config.settings, "database_url", url)
    cfg = _cfg(url)

    command.upgrade(cfg, "0006_source_policy")
    cols, tables, _ = _shape(url)
    assert not NEW_COLUMNS & cols and "source_feeds" not in tables
    engine = make_engine(url)
    with engine.begin() as con:
        con.exec_driver_sql(
            "INSERT INTO channel_workflows (id, name, mode, status, config, created_at, updated_at) VALUES "
            f"('w1','n','auto','active','{{\"source\": {{\"video_id\": \"abcdefghijk\"}}}}',{TS},{TS}), "
            f"('w2','m','auto','active','{{}}',{TS},{TS}), "
            f"('w3','bad','auto','active','not json',{TS},{TS})")
        for pid, wf in (("p1", "w1"), ("p2", "w2"), ("p3", "w3")):
            con.exec_driver_sql(
                "INSERT INTO story_projects (id, title, status, channel_workflow_id, created_at, updated_at, "
                f"source_attempts) VALUES ('{pid}','t','active','{wf}',{TS},{TS},0)")
    engine.dispose()

    command.upgrade(cfg, "head")
    cols, tables, indexes = _shape(url)
    assert NEW_COLUMNS <= cols and "source_feeds" in tables and "ix_story_projects_video_id" in indexes
    engine = make_engine(url)
    with engine.connect() as con:
        got = dict(con.exec_driver_sql("SELECT id, video_id FROM story_projects").fetchall())
        assert got == {"p1": "abcdefghijk", "p2": None, "p3": None}   # only a real workflow source is backfilled
        assert con.exec_driver_sql("SELECT feed_id, source_config FROM story_projects WHERE id='p1'").one() == (
            None, None)
    engine.dispose()

    command.downgrade(cfg, "0006_source_policy")
    cols, tables, _ = _shape(url)
    assert not NEW_COLUMNS & cols and "source_feeds" not in tables
    command.upgrade(cfg, "head")
    assert NEW_COLUMNS <= _shape(url)[0]
