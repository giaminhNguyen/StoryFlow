"""Phase 4 migration gate: 0004_pipeline_dedupe is linear after 0003 and cycles cleanly."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

import storyflow.config
from storyflow.database import make_engine
from storyflow import models  # noqa: F401  (register tables before the db fixture's create_all)

BACKEND = Path(__file__).resolve().parent.parent
NEW_INDEXES = {
    "story_generations": "uq_story_generations_live_input",
    "tts_generations": "uq_tts_generations_live_input",
}


def _cfg(url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _index_names(url, table):
    engine = make_engine(url)
    try:
        return {i["name"] for i in inspect(engine).get_indexes(table)}
    finally:
        engine.dispose()


def test_chain_is_linear_and_0004_follows_0003():
    script = ScriptDirectory.from_config(_cfg("sqlite://"))
    assert len(script.get_heads()) == 1
    rev = script.get_revision("0004_pipeline_dedupe")
    assert rev.down_revision == "0003_story_domain"
    assert script.get_revision("0005_control_plane").down_revision == "0004_pipeline_dedupe"
    assert script.get_revision("0006_source_policy").down_revision == "0005_control_plane"
    assert script.get_revision("0007_source_feeds").down_revision == "0006_source_policy"
    assert script.get_revision("0008_story_reviews").down_revision == "0007_source_feeds"
    assert script.get_heads() == ["0008_story_reviews"]


def test_clean_upgrade_downgrade_upgrade(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'm4.db').as_posix()}"
    monkeypatch.setattr(storyflow.config.settings, "database_url", url)
    cfg = _cfg(url)

    command.upgrade(cfg, "head")
    for table, name in NEW_INDEXES.items():
        assert name in _index_names(url, table)

    command.downgrade(cfg, "0003_story_domain")
    for table, name in NEW_INDEXES.items():
        assert name not in _index_names(url, table)

    command.upgrade(cfg, "head")
    for table, name in NEW_INDEXES.items():
        assert name in _index_names(url, table)


def test_live_generation_index_blocks_duplicate_but_allows_retry_after_failure(db):
    from sqlalchemy.exc import IntegrityError

    from storyflow.models import (
        CanonAnalysis, SourceSnapshot, StoryGeneration, StoryProject, StoryVersion, TTSGeneration,
    )

    project = StoryProject(title="p", slug="p-mig")
    db.add(project)
    db.commit()
    snap = SourceSnapshot(story_project_id=project.id, title="s", content="c")
    db.add(snap)
    db.commit()

    canon = CanonAnalysis(source_snapshot_id=snap.id, status="completed")
    db.add(canon)
    db.commit()

    def gen(status):
        db.add(StoryGeneration(story_project_id=project.id, source_snapshot_id=snap.id,
                               canon_analysis_id=canon.id, status=status))
        db.commit()

    version = StoryVersion(story_project_id=project.id, title="v", content="text")
    db.add(version)
    db.commit()

    def tts(status):
        t = TTSGeneration(story_version_id=version.id, voice="v1", engine="e1", status=status)
        db.add(t)
        db.commit()

    tts("failed")
    tts("failed")            # failed rows never collide
    tts("queued")
    with pytest.raises(IntegrityError):
        tts("processing")    # second live row for same (version, voice, engine)
    db.rollback()
    with pytest.raises(IntegrityError):
        tts("completed")
    db.rollback()

    gen("failed")
    gen("failed")
    gen("processing")
    with pytest.raises(IntegrityError):
        gen("completed")
    db.rollback()
