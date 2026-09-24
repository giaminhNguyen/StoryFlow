"""Verified, non-destructive restore.

Sequence: (1) verify the backup completely (manifest, format, every sha256, DB integrity, known
revision <= code head) - nothing is touched if this fails; (2) refuse targets that are in use or
non-empty (unless ``force``); (3) stage the DB and artifact tree in temp siblings of the targets while
re-hashing what is copied; (4) swap in with os.replace. With ``force`` the existing DB (+ -wal/-shm) and
artifact directory are MOVED to ``<target>.pre-restore-<ts>`` first - nothing is ever deleted - and any
failure during the swap moves everything back. (5) post-check: integrity_check + every artifact path the
DB references exists. Restore never runs migrations (run ``python -m storyflow migrate`` afterwards for an
older backup).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .common import (ARTIFACTS_DIR_NAME, FORMAT_VERSION, OpsError, OpsResult, connect, copy_hash, default_artifact_root,
                     default_db_path, inspect_db, integrity, read_manifest, referenced_artifact_paths, revision_order,
                     safe_rel, sha256_file, timestamp)


@dataclass
class RestoreResult(OpsResult):
    schema_revision: str | None = None
    moved_aside: list[str] = field(default_factory=list)
    missing_artifacts: list[str] = field(default_factory=list)
    counts: dict | None = None


def verify_backup(backup_dir: Path | str) -> dict:
    """Return the manifest of a fully verified backup or raise OpsError (exit 1 corrupt/incomplete,
    exit 2 refused e.g. newer schema)."""
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        raise OpsError(f"backup directory {backup_dir} does not exist", 2)
    manifest = read_manifest(backup_dir)
    if manifest.get("format_version") != FORMAT_VERSION:
        raise OpsError(f"unsupported backup format_version {manifest.get('format_version')!r} "
                       f"(this build understands {FORMAT_VERSION})", 2)
    try:
        db_meta = manifest["db"]
        db_file = backup_dir / safe_rel(db_meta["file"]).as_posix()
        files = manifest["artifacts"]["files"]
    except (KeyError, TypeError) as e:
        raise OpsError(f"manifest is malformed (missing {e})", 1) from None
    if not db_file.is_file():
        raise OpsError(f"backup is incomplete: database file {db_meta['file']} is missing", 1)
    if sha256_file(db_file) != db_meta.get("sha256"):
        raise OpsError("backup is corrupt: database checksum does not match the manifest", 1)
    for entry in files:
        rel = safe_rel(entry["path"]).as_posix()
        f = backup_dir / ARTIFACTS_DIR_NAME / rel
        if not f.is_file():
            raise OpsError(f"backup is incomplete: artifact {rel} is missing", 1)
        if f.stat().st_size != entry.get("size") or sha256_file(f) != entry.get("sha256"):
            raise OpsError(f"backup is corrupt: artifact {rel} does not match its checksum", 1)
    try:
        problem = integrity(db_file)
    except sqlite3.Error as e:
        raise OpsError(f"backup database is unreadable: {e}", 1) from None
    if problem != "ok":
        raise OpsError(f"backup database failed integrity_check: {problem}", 1)
    order = revision_order()
    con = connect(db_file)
    try:
        has = con.execute("SELECT 1 FROM sqlite_master WHERE name='alembic_version'").fetchone()
        row = con.execute("SELECT version_num FROM alembic_version").fetchone() if has else None
    finally:
        con.close()
    rev = row[0] if row else None
    if rev is None:
        raise OpsError("backup database has no alembic revision; refusing to restore it", 2)
    if rev not in order:
        raise OpsError(f"backup schema revision {rev!r} is unknown to this build (newer code?); refusing to restore",
                       2)
    return manifest


def _target_in_use(db_path: Path, timeout: float = 1.0) -> bool:
    con = sqlite3.connect(str(db_path), timeout=timeout, isolation_level=None)
    try:
        try:
            con.execute("BEGIN EXCLUSIVE")
        except sqlite3.OperationalError:
            return True
        con.execute("ROLLBACK")
        return False
    finally:
        con.close()


def _move(src: Path, dst: Path) -> None:
    os.replace(src, dst)


def _nonempty_dir(p: Path) -> bool:
    return p.is_dir() and any(p.iterdir())


def restore_backup(backup_dir: Path | str, db_path: Path | str | None = None,
                   artifact_root: Path | str | None = None, *, force: bool = False,
                   now: datetime | None = None, lock_timeout: float = 1.0) -> RestoreResult:
    try:
        return _restore(Path(backup_dir), Path(db_path) if db_path else default_db_path(),
                        Path(artifact_root) if artifact_root else default_artifact_root(), force, now, lock_timeout)
    except OpsError as e:
        return RestoreResult(ok=False, exit_code=e.exit_code, error=str(e))
    except (sqlite3.Error, OSError) as e:
        return RestoreResult(ok=False, exit_code=1, error=f"restore failed: {e}")


def _restore(backup_dir: Path, db_path: Path, artifact_root: Path, force: bool, now, lock_timeout) -> RestoreResult:
    manifest = verify_backup(backup_dir)  # 1. nothing is touched before this passes
    with_artifacts = bool(manifest["artifacts"].get("included", True))
    db_path, artifact_root = db_path.resolve(), artifact_root.resolve()
    ts = timestamp(now)

    # 2. target safety
    db_has_data = db_path.exists() and inspect_db(db_path).kind != "empty"
    if db_has_data and _target_in_use(db_path, lock_timeout):
        raise OpsError(f"the database {db_path} is in use (locked); stop the app first, then retry", 2)
    art_has_data = with_artifacts and _nonempty_dir(artifact_root)
    if (db_has_data or art_has_data) and not force:
        what = " and ".join(n for n, has in (("database", db_has_data), ("artifact directory", art_has_data)) if has)
        raise OpsError(f"the target {what} already exists and is not empty; re-run with --force to move it aside "
                       f"(to <target>.pre-restore-{ts}) and restore over it", 2)

    # 3. stage next to the targets (same filesystem => atomic os.replace)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    stage_db = db_path.with_name(f".{db_path.name}.restore-{ts}")
    stage_art = artifact_root.with_name(f".{artifact_root.name}.restore-{ts}")
    for p in (stage_db, stage_art):
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    moves: list[tuple[Path, Path]] = []  # (original, aside) for rollback
    try:
        digest, _ = copy_hash(backup_dir / manifest["db"]["file"], stage_db)
        if digest != manifest["db"]["sha256"]:
            raise OpsError("backup database changed while it was being copied; nothing was restored", 1)
        if with_artifacts:
            stage_art.mkdir(parents=True)
            for entry in manifest["artifacts"]["files"]:
                rel = safe_rel(entry["path"]).as_posix()
                digest, _ = copy_hash(backup_dir / ARTIFACTS_DIR_NAME / rel, stage_art / rel)
                if digest != entry["sha256"]:
                    raise OpsError(f"artifact {rel} changed while it was being copied; nothing was restored", 1)

        # 4. swap. Everything existing is moved aside first (never deleted); any failure moves it all back.
        moved_aside: list[str] = []
        try:
            aside_db = db_path.with_name(f"{db_path.name}.pre-restore-{ts}")
            for suffix in ("", "-wal", "-shm"):
                cur = db_path.with_name(db_path.name + suffix)
                if cur.exists():
                    if suffix == "" and not db_has_data:
                        cur.unlink()  # empty placeholder file: nothing to preserve
                        continue
                    dst = aside_db.with_name(aside_db.name + suffix)
                    _move(cur, dst)
                    moves.append((cur, dst))
                    moved_aside.append(str(dst))
            if with_artifacts and artifact_root.exists():
                if art_has_data:
                    aside_art = artifact_root.with_name(f"{artifact_root.name}.pre-restore-{ts}")
                    _move(artifact_root, aside_art)
                    moves.append((artifact_root, aside_art))
                    moved_aside.append(str(aside_art))
                else:
                    artifact_root.rmdir()
            _move(stage_db, db_path)
            if with_artifacts:
                _move(stage_art, artifact_root)
        except OSError as e:
            for orig, aside in reversed(moves):
                if orig.exists():
                    if orig.is_dir():
                        shutil.rmtree(orig)
                    else:
                        orig.unlink()
                os.replace(aside, orig)
            hint = " (is the app still running? stop it first)" if isinstance(e, PermissionError) else ""
            raise OpsError(f"could not swap the restored data in; the previous state was left intact: {e}{hint}",
                           1) from None
    finally:
        if stage_db.exists():
            stage_db.unlink()
        if stage_art.exists():
            shutil.rmtree(stage_art, ignore_errors=True)

    # 5. post-checks
    result = RestoreResult(schema_revision=manifest.get("schema_revision"), moved_aside=moved_aside,
                           counts=manifest.get("counts"))
    problem = integrity(db_path)
    if problem != "ok":
        result.ok, result.exit_code = False, 1
        result.error = f"restored database failed integrity_check: {problem}"
        return result
    con = connect(db_path)
    try:
        referenced = referenced_artifact_paths(con)
    finally:
        con.close()
    missing = []
    for rel in referenced:
        try:
            if not (artifact_root / safe_rel(rel).as_posix()).is_file():
                missing.append(rel)
        except OpsError:
            missing.append(rel)
    if missing:
        result.ok, result.exit_code = False, 1
        result.missing_artifacts = missing
        result.warnings.append(f"{len(missing)} artifact(s) referenced by the database are missing from "
                               f"{artifact_root} (first: {missing[0]})")
    if manifest.get("schema_revision") != manifest.get("code_head"):
        result.warnings.append("the restored database is at an older schema revision; run "
                               "`python -m storyflow migrate` before starting the app")
    return result
