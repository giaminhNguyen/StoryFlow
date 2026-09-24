"""Consistent backup of the SQLite database and the artifact tree.

ORDER MATTERS (documented contract): the DATABASE is snapshotted FIRST, the artifacts SECOND.
Artifacts are append-only and written before the DB row that references them, so every artifact
referenced by the DB snapshot already exists when the artifact copy runs; artifacts created after the
snapshot are merely extra (unreferenced) files. Both are safe to take while the app is running.

The DB uses the sqlite3 online backup API (a consistent page-level snapshot even under concurrent
writers; WAL is fine). It is written to a temp name, integrity-checked, and renamed; the whole backup
directory is staged next to its destination and renamed into place last, so a crash never leaves a
plausible-looking half backup. An existing destination is never overwritten.

The DB and the artifact directory belong together: restore them TOGETHER.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .common import (ARTIFACTS_DIR_NAME, DB_FILE_NAME, FORMAT_VERSION, MANIFEST_NAME, OpsError, OpsResult,
                     code_head, connect, copy_hash, db_counts, default_artifact_root, default_backup_dir,
                     default_db_path, integrity, is_within, iter_artifact_files, referenced_artifact_paths,
                     safe_rel, sanitize_label, sha256_file, timestamp)

import json


@dataclass
class BackupResult(OpsResult):
    path: str | None = None
    manifest: dict | None = None
    missing_referenced: list[str] = field(default_factory=list)


def _check_destination(dest: Path, db_path: Path, artifact_root: Path | None) -> None:
    if dest.exists():
        raise OpsError(f"backup destination {dest} already exists; backups are never overwritten", 2)
    if artifact_root is not None and (is_within(dest, artifact_root) or is_within(artifact_root, dest)):
        raise OpsError(f"backup destination {dest} overlaps the artifact directory {artifact_root}; "
                       "choose a location outside it", 2)
    if is_within(db_path, dest) or dest.resolve() == db_path.resolve().parent:
        raise OpsError(f"backup destination {dest} contains the database being backed up; "
                       "choose a sub-directory such as runtime/backups", 2)


def _snapshot_db(db_path: Path, out_file: Path) -> None:
    """Online backup to ``out_file.tmp`` then atomic rename. The copy is left in a single-file
    (journal_mode=DELETE) state so it has no -wal/-shm companions."""
    tmp = out_file.with_name(out_file.name + ".tmp")
    src = connect(db_path, timeout=30)
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)
            dst.execute("PRAGMA journal_mode=DELETE")
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    os.replace(tmp, out_file)


def create_backup(db_path: Path | str | None = None, artifact_root: Path | str | None = None,
                  dest: Path | str | None = None, *, backup_dir: Path | str | None = None,
                  label: str | None = None, include_artifacts: bool = True,
                  now: datetime | None = None) -> BackupResult:
    """Back up ``db_path`` (+ ``artifact_root`` unless ``include_artifacts`` is False).

    ``dest`` is the exact backup directory; otherwise ``<backup_dir>/backup-YYYYmmdd-HHMMSS[-label]``.
    Never raises for operational problems: see ``ok`` / ``exit_code`` / ``error``."""
    try:
        return _create_backup(Path(db_path) if db_path else default_db_path(),
                              Path(artifact_root) if artifact_root else default_artifact_root(),
                              Path(dest) if dest else None,
                              Path(backup_dir) if backup_dir else default_backup_dir(),
                              label, include_artifacts, now)
    except OpsError as e:
        return BackupResult(ok=False, exit_code=e.exit_code, error=str(e))
    except (sqlite3.Error, OSError) as e:
        return BackupResult(ok=False, exit_code=1, error=f"backup failed: {e}")


def _create_backup(db_path, artifact_root, dest, backup_dir, label, include_artifacts, now) -> BackupResult:
    if not db_path.is_file() or db_path.stat().st_size == 0:
        raise OpsError(f"database {db_path} does not exist or is empty; nothing to back up", 2)
    if dest is None:
        name = f"backup-{timestamp(now)}"
        clean = sanitize_label(label)
        dest = backup_dir / (f"{name}-{clean}" if clean else name)
    _check_destination(dest, db_path, artifact_root if include_artifacts else None)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = dest.parent / f".{dest.name}.partial-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        # 1. DATABASE first.
        db_out = staging / DB_FILE_NAME
        _snapshot_db(db_path, db_out)
        problem = integrity(db_out, quick=True)
        if problem != "ok":
            raise OpsError(f"the database snapshot failed its integrity check ({problem}); the source database "
                           "may be corrupt", 1)
        con = connect(db_out)
        try:
            counts = db_counts(con)
            referenced = referenced_artifact_paths(con)
            revision = None
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='alembic_version'").fetchone():
                row = con.execute("SELECT version_num FROM alembic_version").fetchone()
                revision = row[0] if row else None
        finally:
            con.close()
        # 2. ARTIFACTS second.
        files = []
        if include_artifacts and artifact_root.is_dir():
            for rel, src in iter_artifact_files(artifact_root):
                try:
                    digest, size = copy_hash(src, staging / ARTIFACTS_DIR_NAME / rel)
                except FileNotFoundError:
                    continue  # removed between listing and copy (transient temp file)
                files.append({"path": rel, "sha256": digest, "size": size})
        present = {f["path"] for f in files}
        missing = []
        if include_artifacts:
            for rel in referenced:
                try:
                    if safe_rel(rel).as_posix() not in present:
                        missing.append(rel)
                except OpsError:
                    missing.append(rel)
        manifest = {
            "format_version": FORMAT_VERSION,
            "created_at": (now or datetime.now()).replace(microsecond=0).isoformat(),
            "storyflow_version": "0.9",
            "schema_revision": revision,
            "code_head": code_head(),
            "db": {"file": DB_FILE_NAME, "sha256": sha256_file(db_out), "size": db_out.stat().st_size},
            "artifacts": {"included": include_artifacts, "count": len(files),
                          "total_bytes": sum(f["size"] for f in files), "files": files},
            "counts": counts,
        }
        (staging / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        os.replace(staging, dest)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    warnings = ["the artifact directory and the database must be restored together"] if include_artifacts else [
        "database-only backup: artifacts were NOT included"]
    if missing:
        warnings.append(f"{len(missing)} artifact(s) referenced by the database were missing from the artifact "
                        f"directory at backup time (first: {missing[0]})")
    return BackupResult(path=str(dest), manifest=manifest, warnings=warnings, missing_referenced=missing)
