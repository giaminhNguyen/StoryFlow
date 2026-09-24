"""Shared helpers for the operations toolkit (no printing, no global side effects).

Everything here works on plain SQLite files and directories. Nothing in this package imports the
runtime/app stack at module import time; alembic is only touched through ``alembic_config``.
"""

from __future__ import annotations

import gc
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from ..config import RUNTIME_DIR, settings

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DB_FILE_NAME = "storyflow.db"
ARTIFACTS_DIR_NAME = "artifacts"
_ALEMBIC_LOCK = threading.Lock()
_LABEL_RE = re.compile(r"[^A-Za-z0-9_.-]+")


class OpsError(Exception):
    """An operation was refused (exit_code 2) or failed (exit_code 1); the message is operator-facing."""

    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


@dataclass
class OpsResult:
    ok: bool = True
    exit_code: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


def timestamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def sanitize_label(label: str | None) -> str:
    return _LABEL_RE.sub("-", label).strip("-.") if label else ""


# ---------------------------------------------------------------------------- paths / urls


def db_path_from_url(url: str) -> Path:
    if not url.startswith("sqlite:///"):
        raise OpsError(f"only sqlite databases are supported (got {url.split('@')[-1]!r})", 2)
    raw = url[len("sqlite:///"):]
    if not raw or raw == ":memory:":
        raise OpsError("an in-memory database cannot be operated on", 2)
    return Path(raw)


def url_from_path(path: Path) -> str:
    return f"sqlite:///{Path(path).resolve().as_posix()}"


def default_db_path() -> Path:
    return db_path_from_url(settings.database_url)


def default_artifact_root() -> Path:
    return RUNTIME_DIR / "artifacts"


def default_backup_dir() -> Path:
    return RUNTIME_DIR / "backups"


def is_within(child: Path, parent: Path) -> bool:
    """True when ``child`` equals or lives below ``parent`` (both resolved)."""
    child, parent = Path(child).resolve(), Path(parent).resolve()
    return child == parent or child.is_relative_to(parent)


def safe_rel(rel: str) -> PurePosixPath:
    """Validate a manifest/DB relative artifact path; OpsError on absolute or traversal paths."""
    p = PurePosixPath(str(rel).replace("\\", "/"))
    if not rel or p.is_absolute() or ".." in p.parts or "" in p.parts or (p.parts and ":" in p.parts[0]):
        raise OpsError(f"unsafe artifact path {rel!r}", 1)
    return p


# ---------------------------------------------------------------------------- hashing / copying


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def copy_hash(src: Path, dst: Path) -> tuple[str, int]:
    """Copy ``src`` to ``dst`` (parents created) and return (sha256 of the bytes written, size)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    size = 0
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        for block in iter(lambda: fin.read(1 << 20), b""):
            h.update(block)
            fout.write(block)
            size += len(block)
        fout.flush()
        os.fsync(fout.fileno())
    shutil.copystat(src, dst, follow_symlinks=True)
    return h.hexdigest(), size


def is_transient_artifact(name: str) -> bool:
    return name.startswith(".tmp-") or name.endswith(".part")


def iter_artifact_files(root: Path):
    """Yield (relative posix path, absolute Path) for every regular artifact file, skipping in-flight
    temp files (``.tmp-*`` from ArtifactStore.write, ``*.part``)."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if is_transient_artifact(name):
                continue
            full = Path(dirpath) / name
            yield full.relative_to(root).as_posix(), full


# ---------------------------------------------------------------------------- alembic


def alembic_config(url: str):
    from alembic.config import Config

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def revision_order() -> list[str]:
    """Known revisions, oldest first (the last one is the code head)."""
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config("sqlite://"))
    return [s.revision for s in reversed(list(script.walk_revisions()))]


def code_head() -> str:
    return revision_order()[-1]


def upgrade_to_head(url: str) -> None:
    """``alembic upgrade head`` in-process. env.py reads ``settings.database_url``, so it is swapped
    under a lock for the duration (same technique as runtime.app.ensure_schema)."""
    from alembic import command

    with _ALEMBIC_LOCK:
        previous = settings.database_url
        settings.database_url = url
        try:
            command.upgrade(alembic_config(url), "head")
        finally:
            settings.database_url = previous
    # alembic/env.py never disposes its engine; collect it so the DB file handle is released now
    # (an open handle would block moving/replacing the file on Windows).
    gc.collect()


# ---------------------------------------------------------------------------- DB inspection

MISSING, EMPTY, AT_HEAD, OLDER, NEWER, UNKNOWN, NO_ALEMBIC = (
    "missing", "empty", "at_head", "older", "newer", "unknown", "no_alembic")


@dataclass
class DbState:
    kind: str
    revision: str | None
    head: str
    tables: list[str] = field(default_factory=list)


def connect(path: Path, timeout: float = 5.0) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=timeout)


def table_names(con: sqlite3.Connection) -> list[str]:
    rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return sorted(r[0] for r in rows)


def inspect_db(path: Path) -> DbState:
    """Classify a database file. Never creates or modifies anything. sqlite3.Error (corrupt file) propagates."""
    order = revision_order()
    head = order[-1]
    path = Path(path)
    if not path.exists():
        return DbState(MISSING, None, head)
    if path.stat().st_size == 0:
        return DbState(EMPTY, None, head)
    con = connect(path)
    try:
        tables = table_names(con)
        if not tables:
            return DbState(EMPTY, None, head)
        if "alembic_version" not in tables:
            return DbState(NO_ALEMBIC, None, head, tables)
        row = con.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        con.close()
    rev = row[0] if row else None
    if rev is None:
        return DbState(NO_ALEMBIC, None, head, tables)
    if rev not in order:
        return DbState(UNKNOWN, rev, head, tables)
    if rev == head:
        return DbState(AT_HEAD, rev, head, tables)
    return DbState(OLDER, rev, head, tables)


def integrity(path: Path, *, quick: bool = False) -> str:
    """'ok' or the first problem reported by PRAGMA integrity_check / quick_check."""
    con = connect(path)
    try:
        row = con.execute("PRAGMA quick_check" if quick else "PRAGMA integrity_check").fetchone()
    finally:
        con.close()
    return row[0] if row else "no result"


def db_counts(con: sqlite3.Connection) -> dict:
    tables = set(table_names(con))
    out = {}
    for key, table in (("workflows", "channel_workflows"), ("projects", "story_projects"),
                       ("versions", "story_versions"), ("audio_chunks", "audio_chunks")):
        out[key] = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] if table in tables else 0
    return out


def referenced_artifact_paths(con: sqlite3.Connection) -> list[str]:
    """Every relative artifact path the DB points at (story versions, audio chunks, source snapshot meta)."""
    tables = set(table_names(con))
    paths: list[str] = []
    if "story_versions" in tables:
        paths += [r[0] for r in con.execute("SELECT content_path FROM story_versions WHERE content_path IS NOT NULL")]
    if "audio_chunks" in tables:
        paths += [r[0] for r in con.execute("SELECT artifact_path FROM audio_chunks WHERE artifact_path IS NOT NULL")]
    if "source_snapshots" in tables:
        for (meta,) in con.execute("SELECT meta FROM source_snapshots WHERE meta IS NOT NULL"):
            try:
                value = (json.loads(meta) or {}).get("artifact_path") if isinstance(meta, str) else None
            except ValueError:
                value = None
            if value:
                paths.append(value)
    return sorted(set(p for p in paths if p))


def read_manifest(backup_dir: Path) -> dict:
    path = Path(backup_dir) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise OpsError(f"{backup_dir} has no {MANIFEST_NAME}; it is not a StoryFlow backup", 1) from None
    except (OSError, ValueError) as e:
        raise OpsError(f"cannot read {path}: {e}", 1) from None
    if not isinstance(data, dict):
        raise OpsError(f"{path} is malformed", 1)
    return data
