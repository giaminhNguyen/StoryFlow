"""Operations toolkit: safe migrate (real SQLite files, real alembic, no network)."""

import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

import storyflow.ops.backup as backup_mod
import storyflow.ops.migrate as migrate_mod
from storyflow.__main__ import main
from storyflow.ops import migrate_database, restore_backup, verify_backup
from storyflow.ops.common import code_head
from test_ops_support import insert_v3_rows, sha, upgrade_to


def revision(db):
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def old_db(monkeypatch, tmp_path):
    db = tmp_path / "rt" / "storyflow.db"
    db.parent.mkdir()
    upgrade_to(monkeypatch, db, "0003_story_domain")
    insert_v3_rows(db)
    return db


def test_older_db_is_backed_up_verified_then_upgraded_with_data_intact(old_db, tmp_path):
    bdir = tmp_path / "backups"
    res = migrate_database(old_db, bdir)
    assert res.ok and res.action == "upgraded" and res.exit_code == 0
    assert res.from_revision == "0003_story_domain" and res.to_revision == code_head() == revision(old_db)
    backup = Path(res.backup_path)
    assert backup.parent == bdir and backup.name.startswith("pre-migrate-0003_story_domain-to-0005_control_plane-")
    manifest = verify_backup(backup)
    assert manifest["schema_revision"] == "0003_story_domain" and manifest["counts"]["workflows"] == 1
    con = sqlite3.connect(str(old_db))
    try:
        assert con.execute("SELECT name, status, status_reason, status_detail, client_key FROM channel_workflows "
                           "WHERE id='w1'").fetchone() == ("chan", "active", None, None, None)
        assert con.execute("SELECT title, content_path FROM story_versions WHERE id='v1'").fetchone() == ("T", "s/v1.md")
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()
    # the kept backup is still at the old revision (never modified)
    assert revision(backup / "storyflow.db") == "0003_story_domain"


def test_backup_failure_aborts_before_touching_db(old_db, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(backup_mod, "_snapshot_db", boom)
    before = sha(old_db)
    res = migrate_database(old_db, tmp_path / "backups")
    assert not res.ok and res.exit_code == 1 and res.action == "failed" and "backup failed" in res.error
    assert sha(old_db) == before and revision(old_db) == "0003_story_domain"
    assert not list((tmp_path / "backups").glob("pre-migrate-*")) if (tmp_path / "backups").exists() else True


def test_missing_and_empty_create_at_head_then_noop(tmp_path):
    db = tmp_path / "new" / "storyflow.db"
    plan = migrate_database(db, tmp_path / "b", dry_run=True)
    assert plan.ok and plan.action == "planned" and not db.exists() and not db.parent.exists()
    res = migrate_database(db, tmp_path / "b")
    assert res.ok and res.action == "created" and revision(db) == code_head() and res.backup_path is None
    again = migrate_database(db, tmp_path / "b")
    assert again.ok and again.action == "up_to_date" and "up to date" in again.message
    assert not (tmp_path / "b").exists()
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert migrate_database(empty, tmp_path / "b").action == "created" and revision(empty) == code_head()


def test_newer_or_unknown_revision_refused(old_db, tmp_path):
    con = sqlite3.connect(str(old_db))
    con.execute("UPDATE alembic_version SET version_num='0099_future'")
    con.commit()
    con.close()
    before = sha(old_db)
    res = migrate_database(old_db, tmp_path / "b")
    assert not res.ok and res.exit_code == 2 and res.action == "refused" and "0099_future" in res.error
    assert sha(old_db) == before and not (tmp_path / "b").exists()


def test_non_empty_without_alembic_version_refused(tmp_path):
    db = tmp_path / "foreign.db"
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE precious (x)")
    con.execute("INSERT INTO precious VALUES (1)")
    con.commit()
    con.close()
    before = sha(db)
    res = migrate_database(db, tmp_path / "b")
    assert not res.ok and res.exit_code == 2 and "alembic_version" in res.error and sha(db) == before


def test_garbage_file_refused(tmp_path):
    db = tmp_path / "junk.db"
    db.write_bytes(b"this is not sqlite" * 100)
    res = migrate_database(db, tmp_path / "b")
    assert not res.ok and res.exit_code == 2 and db.read_bytes() == b"this is not sqlite" * 100


def test_dry_run_changes_nothing(old_db, tmp_path):
    before = sha(old_db)
    res = migrate_database(old_db, tmp_path / "b", dry_run=True)
    assert res.ok and res.action == "planned" and res.from_revision == "0003_story_domain"
    assert res.backup_path and "pre-migrate-0003_story_domain-to-" in res.backup_path
    assert sha(old_db) == before and not (tmp_path / "b").exists()


def test_alembic_failure_keeps_backup_and_prints_restore_command(old_db, tmp_path, monkeypatch):
    def failing(url):
        raise OperationalError("ALTER TABLE", {}, sqlite3.OperationalError("boom"))

    monkeypatch.setattr(migrate_mod, "upgrade_to_head", failing)
    res = migrate_database(old_db, tmp_path / "b")
    assert not res.ok and res.exit_code == 1 and res.action == "failed"
    assert Path(res.backup_path).is_dir() and verify_backup(res.backup_path)
    assert res.restore_command.startswith("python -m storyflow restore --from ")
    assert res.backup_path.replace("\\", "/") in res.restore_command.replace("\\", "/")
    assert res.restore_command in res.error and str(old_db) in res.restore_command
    # the advertised recovery works: restoring the kept backup over the DB brings back the old revision
    arts = tmp_path / "arts"
    (arts / "s").mkdir(parents=True)
    (arts / "s" / "v1.md").write_text("body")  # DB-only backup: artifacts stay where they are
    fixed = restore_backup(res.backup_path, old_db, arts, force=True)
    assert fixed.ok and revision(old_db) == "0003_story_domain"


def test_no_auto_backup_warns(old_db, tmp_path):
    res = migrate_database(old_db, tmp_path / "b", auto_backup=False)
    assert res.ok and res.backup_path is None and res.warnings and revision(old_db) == code_head()
    assert not (tmp_path / "b").exists()


def test_cli_migrate_exit_codes(old_db, tmp_path, capsys):
    args = ["migrate", "--db", str(old_db), "--backup-dir", str(tmp_path / "b")]
    assert main([*args, "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"] == "planned"
    assert main(args) == 0
    assert "upgraded" in capsys.readouterr().out
    assert main(args) == 0 and "up to date" in capsys.readouterr().out
    con = sqlite3.connect(str(old_db))
    con.execute("UPDATE alembic_version SET version_num='0099_future'")
    con.commit()
    con.close()
    assert main(args) == 2
    assert main(["migrate", "--database-url", "postgresql://x/y"]) == 2
