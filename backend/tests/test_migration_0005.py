"""Phase 5 migration gate: 0005_control_plane is linear after 0004 and cycles cleanly."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

import storyflow.config
from storyflow.database import make_engine

BACKEND = Path(__file__).resolve().parent.parent


def _cfg(url):
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _snapshot(url):
    engine = make_engine(url)
    try:
        insp = inspect(engine)
        return (
            {c["name"] for c in insp.get_columns("channel_workflows")},
            {c["name"] for c in insp.get_columns("runner_instances")},
            {i["name"] for i in insp.get_indexes("runner_instances")},
        )
    finally:
        engine.dispose()


def test_clean_cycle_and_data_preserved(monkeypatch, tmp_path):
    url = f"sqlite:///{(tmp_path / 'm5.db').as_posix()}"
    monkeypatch.setattr(storyflow.config.settings, "database_url", url)
    cfg = _cfg(url)

    command.upgrade(cfg, "0004_pipeline_dedupe")
    engine = make_engine(url)
    with engine.begin() as con:
        con.exec_driver_sql(
            "INSERT INTO channel_workflows (id, name, mode, status, config, created_at, updated_at) "
            "VALUES ('w1','n','auto','active','{}','2026-01-01 00:00:00.000000','2026-01-01 00:00:00.000000')")
        con.exec_driver_sql(
            "INSERT INTO runner_instances (id, runner_type, enabled, max_concurrency, active_count, state, "
            "supported_roles, created_at) VALUES ('r1','fake',1,1,0,'ready','[]','2026-01-01 00:00:00.000000')")
    engine.dispose()

    command.upgrade(cfg, "head")
    wf_cols, runner_cols, runner_idx = _snapshot(url)
    assert {"status_reason", "status_detail", "client_key"} <= wf_cols
    assert "external_id" in runner_cols and "uq_runner_instances_external" in runner_idx
    engine = make_engine(url)
    with engine.connect() as con:  # existing rows survive with NULL new columns
        assert con.exec_driver_sql("SELECT status, status_reason FROM channel_workflows WHERE id='w1'").one() == (
            "active", None)
        assert con.exec_driver_sql("SELECT external_id FROM runner_instances WHERE id='r1'").scalar() is None
    engine.dispose()

    command.downgrade(cfg, "0004_pipeline_dedupe")
    wf_cols, runner_cols, runner_idx = _snapshot(url)
    assert not ({"status_reason", "status_detail", "client_key"} & wf_cols)
    assert "external_id" not in runner_cols and "uq_runner_instances_external" not in runner_idx

    command.upgrade(cfg, "head")
    wf_cols, runner_cols, _ = _snapshot(url)
    assert "status_reason" in wf_cols and "external_id" in runner_cols
