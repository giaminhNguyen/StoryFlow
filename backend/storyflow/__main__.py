"""``python -m storyflow <command>``: operations toolkit CLI.

Commands: doctor, backup, restore, migrate. Exit codes: 0 ok, 1 problem found / failure, 2 usage error or
refused. ``--json`` prints machine-readable output. Defaults come from ``storyflow.config`` (runtime/).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .ops.common import OpsError, db_path_from_url, default_db_path


def _paths(p: argparse.ArgumentParser, *, artifacts: bool = True, backups: bool = False) -> None:
    p.add_argument("--database-url", default=None, help="sqlite:///PATH (default: runtime/storyflow.db)")
    p.add_argument("--db", default=None, help="database file path (alternative to --database-url)")
    if artifacts:
        p.add_argument("--artifact-root", default=None, help="artifact directory (default: runtime/artifacts)")
    if backups:
        p.add_argument("--backup-dir", default=None, help="backups parent directory (default: runtime/backups)")
    p.add_argument("--json", action="store_true", help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m storyflow", description="StoryFlow operations toolkit")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check the environment (read-only)")
    _paths(d)
    d.add_argument("--quick", action="store_true", help="skip heavy checks (integrity, bootstrap)")
    d.add_argument("--frontend-dir", default=None)
    d.add_argument("--port", type=int, default=8765)
    d.add_argument("--log-dir", default=None)

    b = sub.add_parser("backup", help="consistent backup of the database and artifacts (safe while running)")
    _paths(b, backups=True)
    b.add_argument("--to", default=None, help="exact backup directory (default: <backup-dir>/backup-<timestamp>)")
    b.add_argument("--label", default=None)
    b.add_argument("--no-artifacts", action="store_true", help="database only")

    r = sub.add_parser("restore", help="verify and restore a backup (app must be stopped)")
    _paths(r)
    r.add_argument("--from", dest="source", required=True, help="backup directory")
    r.add_argument("--force", action="store_true", help="move existing data aside (never deleted) and restore over it")

    m = sub.add_parser("migrate", help="safe schema upgrade with automatic pre-migration backup")
    _paths(m, artifacts=False, backups=True)
    m.add_argument("--auto-backup", dest="auto_backup", action="store_true", default=True)
    m.add_argument("--no-auto-backup", dest="auto_backup", action="store_false")
    m.add_argument("--dry-run", action="store_true")
    return parser


def _db(args) -> Path:
    if args.db and args.database_url:
        raise OpsError("use either --db or --database-url, not both", 2)
    if args.db:
        return Path(args.db)
    if args.database_url:
        return db_path_from_url(args.database_url)
    return default_db_path()


def _emit(args, payload: dict, lines: list[str]) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print("\n".join(lines))


def _cmd_doctor(args) -> int:
    from .ops.doctor import run_doctor

    report = run_doctor(db_path=_db(args), artifact_root=args.artifact_root, frontend_dir=args.frontend_dir,
                        port=args.port, log_dir=args.log_dir, quick=args.quick)
    lines = []
    for c in report.checks:
        lines.append(f"[{c.status:<4}] {c.name}: {c.message}")
        if c.fix and c.status in ("WARN", "FAIL"):
            lines.append(f"         fix: {c.fix}")
    fails = sum(c.status == "FAIL" for c in report.checks)
    warns = sum(c.status == "WARN" for c in report.checks)
    lines.append(f"doctor: {'OK' if report.ok else 'PROBLEMS FOUND'} ({fails} fail, {warns} warn)")
    _emit(args, report.to_dict(), lines)
    return report.exit_code


def _cmd_backup(args) -> int:
    from .ops.backup import create_backup

    res = create_backup(_db(args), args.artifact_root, args.to, backup_dir=args.backup_dir, label=args.label,
                        include_artifacts=not args.no_artifacts)
    lines = []
    if res.ok:
        m = res.manifest
        lines.append(f"backup created: {res.path}")
        lines.append(f"  database: {m['db']['size']} bytes, revision {m['schema_revision']}")
        a = m["artifacts"]
        lines.append(f"  artifacts: {a['count']} files, {a['total_bytes']} bytes" if a["included"]
                     else "  artifacts: not included")
        lines.append("  rows: " + ", ".join(f"{k}={v}" for k, v in m["counts"].items()))
        lines += [f"  note: {w}" for w in res.warnings]
    else:
        lines.append(f"backup FAILED: {res.error}")
    _emit(args, dataclasses.asdict(res), lines)
    return res.exit_code


def _cmd_restore(args) -> int:
    from .ops.restore import restore_backup

    res = restore_backup(args.source, _db(args), args.artifact_root, force=args.force)
    lines = []
    if res.error:
        lines.append(f"restore FAILED: {res.error}")
    else:
        lines.append(f"restore complete (schema revision {res.schema_revision})")
    lines += [f"  moved aside (kept): {p}" for p in res.moved_aside]
    lines += [f"  missing artifact: {p}" for p in res.missing_artifacts[:20]]
    lines += [f"  warning: {w}" for w in res.warnings]
    _emit(args, dataclasses.asdict(res), lines)
    return res.exit_code


def _cmd_migrate(args) -> int:
    from .ops.migrate import migrate_database

    res = migrate_database(_db(args), args.backup_dir, auto_backup=args.auto_backup, dry_run=args.dry_run)
    lines = []
    if res.action == "planned":
        lines.append(f"dry run: {res.message}")
    elif res.error:
        lines.append(f"migrate {res.action.upper()}: {res.error}")
    else:
        lines.append(res.message)
        if res.backup_path:
            lines.append(f"  pre-migrate backup kept at: {res.backup_path}")
    lines += [f"  warning: {w}" for w in res.warnings]
    _emit(args, dataclasses.asdict(res), lines)
    return res.exit_code


_COMMANDS = {"doctor": _cmd_doctor, "backup": _cmd_backup, "restore": _cmd_restore, "migrate": _cmd_migrate}


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:  # argparse: usage errors are exit code 2, --help is 0
        return e.code if isinstance(e.code, int) else 2
    try:
        return _COMMANDS[args.command](args)
    except OpsError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code


if __name__ == "__main__":
    sys.exit(main())
