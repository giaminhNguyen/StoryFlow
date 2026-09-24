"""Shared helpers for the test_ops_* modules (no tests here)."""

import gc
import hashlib
import json
import sqlite3
from pathlib import Path

from alembic import command

import storyflow.config
from storyflow.ops.common import alembic_config, url_from_path
from test_phase5_integration import Stack

NOW_SQL = "2026-01-01 00:00:00.000000"


def completed_workspace(tmp_path, name="src") -> Stack:
    """A real migrated DB + artifacts with one completed workflow (deterministic fakes). Caller closes ``.app``."""
    s = Stack(tmp_path / name)
    wf = s.new_workflow()
    s.workflows.start(wf)
    s.assign_discovered_runner(wf)
    s.drive(wf, lambda snap: snap.display_state == "completed")
    s.wf_id = wf
    s.db_file = tmp_path / name / "storyflow.db"
    s.art_root = tmp_path / name / "artifacts"
    return s


def upgrade_to(monkeypatch, db_path: Path, revision: str) -> None:
    url = url_from_path(db_path)
    monkeypatch.setattr(storyflow.config.settings, "database_url", url)
    command.upgrade(alembic_config(url), revision)
    gc.collect()  # env.py leaks its engine; release the file handle


def insert_v3_rows(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    try:
        con.execute("INSERT INTO channel_workflows (id, name, mode, status, config, created_at, updated_at) "
                    f"VALUES ('w1','chan','auto','active','{{}}','{NOW_SQL}','{NOW_SQL}')")
        con.execute("INSERT INTO story_projects (id, channel_workflow_id, title, status, created_at, updated_at) "
                    f"VALUES ('p1','w1','Tale','active','{NOW_SQL}','{NOW_SQL}')")
        con.execute("INSERT INTO story_versions (id, story_project_id, version_number, title, content, word_count, "
                    f"content_path, status, created_at, updated_at) VALUES ('v1','p1',1,'T','body',1,'s/v1.md',"
                    f"'active','{NOW_SQL}','{NOW_SQL}')")
        con.commit()
    finally:
        con.close()


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def rewrite_manifest(backup: Path, mutate) -> None:
    m = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    mutate(m)
    (backup / "manifest.json").write_text(json.dumps(m), encoding="utf-8")


def tree_snapshot(root: Path) -> dict:
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): sha(p) for p in sorted(root.rglob("*")) if p.is_file()}
