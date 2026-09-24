"""Safe schema upgrade.

Policy (never drops data, never downgrades, never deletes a backup):
  missing/empty DB                 -> create at head
  older KNOWN revision             -> verified pre-migrate DB backup, then ``alembic upgrade head``, then checks
  at head                          -> nothing to do
  newer / unknown revision         -> refused (exit 2)
  tables but no alembic_version    -> refused (exit 2)
If the backup cannot be taken/verified the DB is left untouched (exit 1). If the upgrade fails the backup is
kept and the exact restore command is reported.
"""

from __future__ import annotations

import shlex
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .backup import create_backup
from .common import (AT_HEAD, EMPTY, MISSING, NEWER, NO_ALEMBIC, OLDER, UNKNOWN, OpsError, OpsResult, default_backup_dir,
                     default_db_path, inspect_db, integrity, timestamp, upgrade_to_head, url_from_path)
from .restore import verify_backup


@dataclass
class MigrateResult(OpsResult):
    action: str = "none"        # created | upgraded | up_to_date | planned | refused | failed
    from_revision: str | None = None
    to_revision: str | None = None
    backup_path: str | None = None
    restore_command: str | None = None
    message: str = ""


def restore_command(backup: str, db_path: Path) -> str:
    return (f"python -m storyflow restore --from {shlex.quote(str(backup))} "
            f"--db {shlex.quote(str(db_path))} --force")


def _upgrade_errors():
    from alembic.util import CommandError
    from sqlalchemy.exc import SQLAlchemyError

    return (CommandError, SQLAlchemyError, sqlite3.Error, OSError)


def migrate_database(db_path: Path | str | None = None, backup_dir: Path | str | None = None, *,
                     auto_backup: bool = True, dry_run: bool = False,
                     now: datetime | None = None) -> MigrateResult:
    db_path = Path(db_path) if db_path else default_db_path()
    backup_dir = Path(backup_dir) if backup_dir else default_backup_dir()
    try:
        state = inspect_db(db_path)
    except sqlite3.Error as e:
        return MigrateResult(ok=False, exit_code=2, action="refused",
                             error=f"{db_path} is not a readable SQLite database ({e}); refusing to touch it")
    head = state.head
    r = MigrateResult(from_revision=state.revision, to_revision=head)

    if state.kind == AT_HEAD:
        r.action, r.message = "up_to_date", f"database is up to date (revision {head})"
        return r
    if state.kind in (NEWER, UNKNOWN):
        return _refuse(r, f"database revision {state.revision!r} is not known to this code (head {head!r}); it was "
                          "probably written by a newer StoryFlow. Upgrade the code; the database was not touched.")
    if state.kind == NO_ALEMBIC:
        return _refuse(r, f"{db_path} has tables ({', '.join(state.tables[:4])}...) but no alembic_version; it was not "
                          "created by StoryFlow migrations. Refusing to modify it.")

    url = url_from_path(db_path)
    if state.kind in (MISSING, EMPTY):
        r.message = f"database is {state.kind}; will be created at revision {head}"
        if dry_run:
            r.action = "planned"
            return r
        try:
            upgrade_to_head(url)
        except _upgrade_errors() as e:
            return _failed(r, f"creating the database failed: {e}")
        return _verify(r, db_path, "created", f"database created at revision {head}")

    # OLDER
    plan_backup = None
    if auto_backup:
        plan_backup = backup_dir / f"pre-migrate-{state.revision}-to-{head}-{timestamp(now)}"
    r.backup_path = str(plan_backup) if plan_backup else None
    r.message = f"database will be upgraded {state.revision} -> {head}" + (
        f" after a verified backup to {plan_backup}" if plan_backup else " WITHOUT a backup (--no-auto-backup)")
    if dry_run:
        r.action = "planned"
        return r
    if plan_backup is not None:
        b = create_backup(db_path, None, plan_backup, include_artifacts=False, now=now)
        if b.ok:
            try:
                verify_backup(plan_backup)
            except OpsError as e:
                return _failed(r, f"pre-migrate backup could not be verified ({e}); the database was not modified")
        else:
            return _failed(r, f"pre-migrate backup failed ({b.error}); the database was not modified")
    else:
        r.warnings.append("no backup was taken (--no-auto-backup)")
    try:
        upgrade_to_head(url)
    except _upgrade_errors() as e:
        if plan_backup is not None:
            r.restore_command = restore_command(str(plan_backup), db_path)
        return _failed(r, f"migration failed: {e}." + (
            f" The pre-migrate backup was kept at {plan_backup}. To go back, stop the app and run: "
            f"{r.restore_command}" if plan_backup is not None else ""))
    return _verify(r, db_path, "upgraded", f"database upgraded {state.revision} -> {head}")


def _refuse(r: MigrateResult, text: str) -> MigrateResult:
    r.ok, r.exit_code, r.action, r.error = False, 2, "refused", text
    return r


def _failed(r: MigrateResult, text: str) -> MigrateResult:
    r.ok, r.exit_code, r.action, r.error = False, 1, "failed", text
    return r


def _verify(r: MigrateResult, db_path: Path, action: str, message: str) -> MigrateResult:
    """Post-migration quick_check + revision check."""
    try:
        state = inspect_db(db_path)
        problem = integrity(db_path, quick=True)
    except sqlite3.Error as e:
        problem, state = str(e), None
    if problem != "ok" or state is None or state.kind != AT_HEAD:
        if r.backup_path:
            r.restore_command = restore_command(r.backup_path, db_path)
        return _failed(r, f"post-migration verification failed ({problem}, revision "
                          f"{state.revision if state else '?'})." + (
            f" Restore with: {r.restore_command}" if r.restore_command else ""))
    r.action, r.message = action, message
    return r
