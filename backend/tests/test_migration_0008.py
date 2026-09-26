"""Story review migration gate: 0008_story_reviews is linear after 0007 and cycles cleanly."""

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


def _shape(url):
    engine = make_engine(url)
    try:
        insp = inspect(engine)
        tables = set(insp.get_table_names())
        indexes = {i["name"] for i in insp.get_indexes("story_reviews")} if "story_reviews" in tables else set()
        return tables, indexes
    finally:
        engine.dispose()


def test_clean_cycle_and_live_review_is_unique_per_version(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'm8.db').as_posix()}"
    monkeypatch.setattr(storyflow.config.settings, "database_url", url)
    cfg = _cfg(url)

    command.upgrade(cfg, "0007_source_feeds")
    assert "story_reviews" not in _shape(url)[0]

    command.upgrade(cfg, "head")
    tables, indexes = _shape(url)
    assert "story_reviews" in tables and {"ix_story_reviews_story_project_id", "uq_story_reviews_live_input"} <= indexes

    engine = make_engine(url)
    with engine.begin() as con:
        con.exec_driver_sql(f"INSERT INTO story_projects (id, title, status, created_at, updated_at, source_attempts) "
                            f"VALUES ('p','t','active',{TS},{TS},0)")
        con.exec_driver_sql(f"INSERT INTO story_versions (id, story_project_id, version_number, title, content, "
                            f"word_count, status, created_at, updated_at) VALUES ('v','p',1,'t','c',1,'active',{TS},{TS})")

        def review(rid, status):
            con.exec_driver_sql(
                "INSERT INTO story_reviews (id, story_project_id, story_version_id, round_number, status, config, "
                f"created_at, updated_at) VALUES ('{rid}','p','v',1,'{status}','{{}}',{TS},{TS})")

        review("r1", "failed")        # a failed review does not block a new one
        review("r2", "queued")
    engine.dispose()

    engine = make_engine(url)
    try:
        with engine.begin() as con:
            try:
                con.exec_driver_sql(
                    "INSERT INTO story_reviews (id, story_project_id, story_version_id, round_number, status, config, "
                    f"created_at, updated_at) VALUES ('r3','p','v',2,'completed','{{}}',{TS},{TS})")
            except Exception as exc:   # noqa: BLE001 - the partial unique index must refuse a 2nd live review
                assert "UNIQUE" in str(exc).upper()
            else:
                raise AssertionError("a second live review of the same version was accepted")
    finally:
        engine.dispose()

    command.downgrade(cfg, "0007_source_feeds")
    assert "story_reviews" not in _shape(url)[0]
    command.upgrade(cfg, "head")
    assert "story_reviews" in _shape(url)[0]
