"""Operations toolkit: backup + restore (real SQLite files under tmp_path, no network)."""

import json
import sqlite3
import threading
from pathlib import Path

import pytest

import storyflow.ops.restore as restore_mod
from storyflow.__main__ import main
from storyflow.artifacts import ArtifactStore
from storyflow.ops import create_backup, restore_backup, verify_backup
from storyflow.ops.common import db_counts, iter_artifact_files, referenced_artifact_paths
from storyflow.readmodels import ReadModels, to_jsonable
from storyflow.runtime import build_runtime
from storyflow.subtitles import FakeSubtitleClient
from test_ops_support import (NOW_SQL, completed_workspace, rewrite_manifest, sha, tree_snapshot, upgrade_to)


@pytest.fixture
def ws(tmp_path):
    s = completed_workspace(tmp_path)
    yield s
    s.app.close()


@pytest.fixture
def backup(ws, tmp_path):
    res = create_backup(ws.db_file, ws.art_root, tmp_path / "bk" / "b1")
    assert res.ok, res.error
    return Path(res.path)


def _refs(db):
    con = sqlite3.connect(str(db))
    try:
        return referenced_artifact_paths(con)
    finally:
        con.close()


def make_target(monkeypatch, tmp_path, name="target"):
    """An existing, non-empty runtime: migrated DB with a marker project + one artifact."""
    root = tmp_path / name
    db = root / "storyflow.db"
    root.mkdir()
    upgrade_to(monkeypatch, db, "head")
    con = sqlite3.connect(str(db))
    con.execute("INSERT INTO story_projects (id, title, status, created_at, updated_at) "
                f"VALUES ('marker','OLD','active','{NOW_SQL}','{NOW_SQL}')")
    con.commit()
    con.close()
    (root / "artifacts").mkdir()
    (root / "artifacts" / "old.txt").write_text("old data", encoding="utf-8")
    return db, root / "artifacts"


# ---------------------------------------------------------------------------- backup


def test_backup_manifest_hashes_and_counts(ws, backup):
    m = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    assert m["format_version"] == 1 and m["schema_revision"] == m["code_head"]
    assert m["db"]["sha256"] == sha(backup / "storyflow.db")
    assert m["artifacts"]["count"] == len(m["artifacts"]["files"]) > 0
    for f in m["artifacts"]["files"]:
        assert f["sha256"] == sha(backup / "artifacts" / f["path"])
        assert f["size"] == (backup / "artifacts" / f["path"]).stat().st_size
    assert m["artifacts"]["total_bytes"] == sum(f["size"] for f in m["artifacts"]["files"])
    assert m["counts"]["workflows"] == 1 and m["counts"]["versions"] >= 1 and m["counts"]["audio_chunks"] >= 1
    assert "T" in m["created_at"] and "+" not in m["created_at"] and not m["created_at"].endswith("Z")  # naive local
    assert not list(backup.glob("*.tmp")) and not list(backup.parent.glob(".*partial*"))
    assert verify_backup(backup)["db"]["sha256"] == m["db"]["sha256"]


def test_backup_consistent_while_writer_inserts(ws, tmp_path):
    (ws.art_root / ".tmp-inflight.md").write_text("half", encoding="utf-8")
    (ws.art_root / "audio" / "x.wav.part").parent.mkdir(exist_ok=True)
    (ws.art_root / "audio" / "x.wav.part").write_text("half", encoding="utf-8")
    stop, started, errors = threading.Event(), threading.Event(), []

    def writer():
        con = sqlite3.connect(str(ws.db_file), timeout=30)
        n = 0
        try:
            while not stop.is_set():
                con.execute("INSERT INTO story_projects (id, title, status, created_at, updated_at) "
                            f"VALUES ('w{n}','w','active','{NOW_SQL}','{NOW_SQL}')")
                con.commit()
                n += 1
                if n == 5:
                    started.set()
        except sqlite3.Error as e:
            errors.append(e)
        finally:
            con.close()

    t = threading.Thread(target=writer)
    t.start()
    try:
        assert started.wait(30)
        res = create_backup(ws.db_file, ws.art_root, tmp_path / "bk" / "live")
    finally:
        stop.set()
        t.join(30)
    assert not errors and res.ok, res.error
    b = Path(res.path)
    verify_backup(b)
    con = sqlite3.connect(str(b / "storyflow.db"))
    try:
        assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        counts = db_counts(con)
        refs = referenced_artifact_paths(con)
        assert counts == res.manifest["counts"] and counts["projects"] >= 5
    finally:
        con.close()
    assert refs and all((b / "artifacts" / r).is_file() for r in refs) and res.missing_referenced == []
    names = [f["path"] for f in res.manifest["artifacts"]["files"]]
    assert not any(".tmp-" in n or n.endswith(".part") for n in names)


def test_backup_refuses_existing_and_nested_destinations(ws, tmp_path):
    existing = tmp_path / "already"
    existing.mkdir()
    r = create_backup(ws.db_file, ws.art_root, existing)
    assert not r.ok and r.exit_code == 2 and "already exists" in r.error
    assert list(existing.iterdir()) == []
    r = create_backup(ws.db_file, ws.art_root, ws.art_root / "nested-backup")
    assert not r.ok and r.exit_code == 2 and "artifact" in r.error
    assert not (ws.art_root / "nested-backup").exists()
    r = create_backup(ws.db_file, ws.art_root, ws.db_file.parent)  # would contain the DB
    assert not r.ok and r.exit_code == 2
    r = create_backup(tmp_path / "missing.db", ws.art_root, tmp_path / "x")
    assert not r.ok and r.exit_code == 2


def test_backup_default_location_and_label(ws, tmp_path):
    r = create_backup(ws.db_file, ws.art_root, backup_dir=tmp_path / "bks", label="before upgrade!")
    assert r.ok
    name = Path(r.path).name
    assert name.startswith("backup-") and name.endswith("-before-upgrade") and Path(r.path).parent == tmp_path / "bks"
    assert any("restored together" in w for w in r.warnings)


def test_backup_no_artifacts(ws, tmp_path):
    r = create_backup(ws.db_file, ws.art_root, tmp_path / "dbonly", include_artifacts=False)
    assert r.ok and not (Path(r.path) / "artifacts").exists()
    assert r.manifest["artifacts"] == {"included": False, "count": 0, "total_bytes": 0, "files": []}
    verify_backup(r.path)


# ---------------------------------------------------------------------------- restore


def test_restore_round_trip_works_with_real_stack(ws, backup, tmp_path):
    db, art = tmp_path / "fresh" / "storyflow.db", tmp_path / "fresh" / "artifacts"
    res = restore_backup(backup, db, art)
    assert res.ok, (res.error, res.warnings)
    assert res.missing_artifacts == [] and res.moved_aside == [] and res.warnings == []
    app = build_runtime(database_url=f"sqlite:///{db.as_posix()}", artifact_root=art, providers=[],
                        subtitle_client=FakeSubtitleClient({}))
    try:
        restored = ReadModels(app.ctx, app.orchestrator.chain).list_workflows()
        original = ReadModels(ws.app.ctx, ws.app.orchestrator.chain).list_workflows()
        assert to_jsonable(restored) == to_jsonable(original) and len(restored) == 1
        snap = ReadModels(app.ctx, app.orchestrator.chain).get_workflow(ws.wf_id)
        assert to_jsonable(snap) == to_jsonable(ws.read.get_workflow(ws.wf_id))
        src_store = ArtifactStore(ws.art_root)
        for rel in _refs(db):
            assert app.store.exists(rel) and app.store.read(rel) == src_store.read(rel)
    finally:
        app.close()
    assert tree_snapshot(art) == tree_snapshot(ws.art_root)


def _tamper_artifact(b):
    f = next((b / "artifacts").rglob("*.*"))
    data = bytearray(f.read_bytes())
    data[0] ^= 0xFF
    f.write_bytes(bytes(data))


def _tamper_db(b):
    p = b / "storyflow.db"
    data = bytearray(p.read_bytes())
    data[-10] ^= 0xFF
    p.write_bytes(bytes(data))


def _delete_artifact(b):
    next((b / "artifacts").rglob("*.*")).unlink()


def _edit_manifest_hash(b):
    rewrite_manifest(b, lambda m: m["artifacts"]["files"][0].__setitem__("sha256", "0" * 64))


def _bad_format(b):
    rewrite_manifest(b, lambda m: m.__setitem__("format_version", 99))


def _no_manifest(b):
    (b / "manifest.json").unlink()


def _traversal(b):
    rewrite_manifest(b, lambda m: m["artifacts"]["files"][0].__setitem__("path", "../evil.txt"))


@pytest.mark.parametrize("tamper", [_tamper_artifact, _tamper_db, _delete_artifact, _edit_manifest_hash,
                                    _bad_format, _no_manifest, _traversal])
def test_tampered_backup_is_refused_and_target_untouched(ws, backup, tmp_path, monkeypatch, tamper):
    db, art = make_target(monkeypatch, tmp_path)
    before = (sha(db), tree_snapshot(art), sorted(p.name for p in db.parent.iterdir()))
    tamper(backup)
    res = restore_backup(backup, db, art, force=True)
    assert not res.ok and res.exit_code in (1, 2) and res.error
    assert (sha(db), tree_snapshot(art), sorted(p.name for p in db.parent.iterdir())) == before


def test_non_empty_target_needs_force_and_force_moves_aside(ws, backup, tmp_path, monkeypatch):
    db, art = make_target(monkeypatch, tmp_path)
    before_db, before_art = sha(db), tree_snapshot(art)
    res = restore_backup(backup, db, art)
    assert not res.ok and res.exit_code == 2 and "--force" in res.error
    assert (sha(db), tree_snapshot(art)) == (before_db, before_art)

    res = restore_backup(backup, db, art, force=True)
    assert res.ok, res.error
    assert len(res.moved_aside) == 2
    aside_db = next(Path(p) for p in res.moved_aside if "storyflow.db" in Path(p).name)
    aside_art = next(Path(p) for p in res.moved_aside if Path(p).name.startswith("artifacts"))
    assert ".pre-restore-" in aside_db.name and ".pre-restore-" in aside_art.name
    assert sha(aside_db) == before_db and tree_snapshot(aside_art) == before_art  # old data still present
    con = sqlite3.connect(str(db))
    assert con.execute("SELECT COUNT(*) FROM story_projects WHERE id='marker'").fetchone()[0] == 0
    con.close()
    # ...and the old state is restorable: the aside DB opens with its marker row
    con = sqlite3.connect(str(aside_db))
    assert con.execute("SELECT title FROM story_projects WHERE id='marker'").fetchone()[0] == "OLD"
    con.close()


@pytest.mark.parametrize("fail_on", ["storyflow.db", "artifacts"])
def test_swap_failure_leaves_old_state_intact(ws, backup, tmp_path, monkeypatch, fail_on):
    db, art = make_target(monkeypatch, tmp_path)
    before = (sha(db), tree_snapshot(art))
    real = restore_mod._move

    def flaky(src, dst):
        if Path(dst).name == fail_on and ".pre-restore-" not in Path(dst).name:
            raise PermissionError("simulated crash mid-swap")
        real(src, dst)

    monkeypatch.setattr(restore_mod, "_move", flaky)
    res = restore_backup(backup, db, art, force=True)
    assert not res.ok and res.exit_code == 1 and "previous state was left intact" in res.error
    assert (sha(db), tree_snapshot(art)) == before
    leftovers = [p.name for p in db.parent.iterdir()]
    assert sorted(leftovers) == ["artifacts", "storyflow.db"]


def test_in_use_database_is_refused(ws, backup, tmp_path, monkeypatch):
    db, art = make_target(monkeypatch, tmp_path)
    before = sha(db)
    holder = sqlite3.connect(str(db), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        res = restore_backup(backup, db, art, force=True, lock_timeout=0.2)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert not res.ok and res.exit_code == 2 and "stop the app" in res.error
    assert sha(db) == before and tree_snapshot(art) == {"old.txt": sha(art / "old.txt")}


def test_backup_with_newer_revision_is_refused(ws, backup, tmp_path):
    con = sqlite3.connect(str(backup / "storyflow.db"))
    con.execute("UPDATE alembic_version SET version_num='9999_from_the_future'")
    con.commit()
    con.close()
    rewrite_manifest(backup, lambda m: m["db"].__setitem__("sha256", sha(backup / "storyflow.db")))
    db, art = tmp_path / "t" / "storyflow.db", tmp_path / "t" / "artifacts"
    res = restore_backup(backup, db, art)
    assert not res.ok and res.exit_code == 2 and "unknown" in res.error
    assert not db.exists() and not art.exists()


def test_missing_referenced_artifacts_reported(ws, tmp_path):
    victim = _refs(ws.db_file)[0]
    # a DB-only backup restored to an EMPTY artifact dir has dangling references -> WARN + exit 1
    db_only = create_backup(ws.db_file, ws.art_root, tmp_path / "bk" / "d", include_artifacts=False)
    res = restore_backup(db_only.path, tmp_path / "r" / "storyflow.db", tmp_path / "r" / "artifacts")
    assert not res.ok and res.exit_code == 1 and victim in res.missing_artifacts and res.warnings


def test_older_revision_backup_restores_with_migrate_hint(monkeypatch, tmp_path):
    old = tmp_path / "old" / "storyflow.db"
    old.parent.mkdir()
    upgrade_to(monkeypatch, old, "0003_story_domain")
    b = create_backup(old, tmp_path / "old" / "artifacts", tmp_path / "bk" / "o", include_artifacts=False)
    assert b.ok
    res = restore_backup(b.path, tmp_path / "n" / "storyflow.db", tmp_path / "n" / "artifacts")
    assert res.ok and any("migrate" in w for w in res.warnings)
    con = sqlite3.connect(str(tmp_path / "n" / "storyflow.db"))
    assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0003_story_domain"  # no migration
    con.close()


# ---------------------------------------------------------------------------- CLI


def test_cli_backup_restore_exit_codes_and_json(ws, tmp_path, capsys):
    dest = tmp_path / "cli-bk"
    common = ["--db", str(ws.db_file), "--artifact-root", str(ws.art_root)]
    assert main(["backup", *common, "--to", str(dest), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] and payload["path"] == str(dest) and payload["manifest"]["counts"]["workflows"] == 1
    assert main(["backup", *common, "--to", str(dest)]) == 2  # exists
    assert "already exists" in capsys.readouterr().out
    target = ["--db", str(tmp_path / "cr" / "s.db"), "--artifact-root", str(tmp_path / "cr" / "a")]
    assert main(["restore", "--from", str(dest), *target]) == 0
    capsys.readouterr()
    assert main(["restore", "--from", str(dest), *target]) == 2  # now non-empty, no --force
    assert main(["restore", "--from", str(tmp_path / "nope"), *target]) == 2
    assert main(["restore"]) == 2  # usage error
    assert main(["backup", "--db", "a", "--database-url", "sqlite:///b"]) == 2


def test_iter_artifact_files_skips_transients(tmp_path):
    (tmp_path / "a").mkdir()
    for n in ("a/ok.txt", "a/.tmp-x.txt", "a/y.part"):
        (tmp_path / n).write_text("x")
    assert [r for r, _ in iter_artifact_files(tmp_path)] == ["a/ok.txt"]
