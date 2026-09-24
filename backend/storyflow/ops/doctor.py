"""``doctor``: read-only environment diagnostics. Never modifies data, loads models or uses the network.

Statuses: PASS, INFO (informational), WARN (works but needs attention), FAIL (must be fixed; exit code 1).
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..config import PROJECT_ROOT, RUNTIME_DIR
from .common import (AT_HEAD, EMPTY, MISSING, NEWER, NO_ALEMBIC, OLDER, UNKNOWN, default_artifact_root, default_db_path,
                     inspect_db, integrity)

PASS, INFO, WARN, FAIL = "PASS", "INFO", "WARN", "FAIL"
MIN_PYTHON = (3, 11)
TESTED_PYTHON = (3, 13)
MIN_FREE_BYTES = 1 << 30
REQUIRED_PACKAGES = ("sqlalchemy", "alembic", "fastapi", "uvicorn")
DEFAULT_PORT = 8765


@dataclass
class Check:
    name: str
    status: str
    message: str
    fix: str = ""


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(c.status == FAIL for c in self.checks)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checks": [asdict(c) for c in self.checks]}


def _probe_writable(directory: Path) -> None:
    """Write + delete a temp file in an existing directory. Raises OSError when not writable."""
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".doctor-")
    os.close(fd)
    os.unlink(tmp)


def _nearest_existing(path: Path) -> Path:
    p = Path(path).resolve()
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _check_dir_writable(name: str, directory: Path, what: str) -> Check:
    existing = _nearest_existing(directory)
    try:
        _probe_writable(existing)
    except OSError as e:
        return Check(name, FAIL, f"{what} {directory} is not writable ({e})",
                     f"Fix permissions on {existing} or choose another location.")
    if existing == Path(directory).resolve():
        return Check(name, PASS, f"{what} {directory} is writable")
    return Check(name, PASS, f"{what} {directory} does not exist yet; it will be created")


def _port_free(port: int) -> Check:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as e:
        return Check("port", WARN, f"port {port} on 127.0.0.1 is not available ({e})",
                     f"Stop the process using port {port} or start the API with --port <other>.")
    finally:
        sock.close()
    return Check("port", PASS, f"port {port} is free")


def _db_checks(db_path: Path, quick: bool) -> list[Check]:
    out = []
    try:
        state = inspect_db(db_path)
    except sqlite3.Error as e:
        return [Check("database", FAIL, f"{db_path} is not a readable SQLite database ({e})",
                      "Restore a backup: python -m storyflow restore --from <backup-dir> --force")]
    k = state.kind
    if k == MISSING:
        out.append(Check("database", PASS, f"{db_path} does not exist yet; it will be created at revision {state.head}"))
        return out
    if k == EMPTY:
        out.append(Check("database", PASS, "database is empty; it will be created on first start or by migrate"))
        return out
    if k == AT_HEAD:
        out.append(Check("database", PASS, f"schema is at head ({state.revision})"))
    elif k == OLDER:
        out.append(Check("database", WARN, f"schema is at {state.revision}, code expects {state.head}",
                         "Run: python -m storyflow migrate (takes a verified backup first)."))
    elif k in (NEWER, UNKNOWN):
        out.append(Check("database", FAIL, f"schema revision {state.revision!r} is unknown to this code "
                                            f"(head {state.head}); the database is newer than the code",
                         "Update StoryFlow to a version that knows this revision, or restore an older backup."))
    elif k == NO_ALEMBIC:
        out.append(Check("database", FAIL, "database has tables but no alembic_version (not a StoryFlow-migrated DB)",
                         "Point --db at an empty file or a StoryFlow database."))
    if quick:
        out.append(Check("database_integrity", INFO, "skipped (--quick)"))
    else:
        try:
            problem = integrity(db_path, quick=True)
        except sqlite3.Error as e:
            problem = str(e)
        if problem == "ok":
            out.append(Check("database_integrity", PASS, "PRAGMA quick_check: ok"))
        else:
            out.append(Check("database_integrity", FAIL, f"PRAGMA quick_check reported: {problem}",
                             "Restore the latest backup: python -m storyflow restore --from <backup-dir> --force"))
    return out


def _bootstrap_check(script: Path | None) -> Check:
    if script is None or not Path(script).is_file():
        return Check("bootstrap", WARN, "bootstrap.py not found; cannot verify pinned dependencies",
                     "Run from a full StoryFlow checkout.")
    try:
        proc = subprocess.run([sys.executable, str(script), "--check"], cwd=str(Path(script).parent),
                              capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return Check("bootstrap", WARN, f"could not run bootstrap --check ({type(e).__name__}: {e})",
                     "Run: python bootstrap.py --check")
    if proc.returncode == 0:
        return Check("bootstrap", PASS, "pinned dependencies (external/, skills/) match sources.lock.json")
    return Check("bootstrap", WARN, "pinned dependencies are missing or out of sync",
                 "Run: python bootstrap.py   (needs git and network; only the real subtitle/skills need it)")


def _provider_checks(env: dict | None, artifact_root: Path) -> list[Check]:
    from ..artifacts import ArtifactStore
    from ..providers import (DISABLED, FAKE, MISCONFIGURED, READY, UNAVAILABLE, build_provider_stack,
                             load_provider_config)

    config = load_provider_config(env, env_files=[] if env is not None else None)
    with tempfile.TemporaryDirectory(prefix="storyflow-doctor-") as tmp:  # keeps the real store untouched
        try:
            statuses = build_provider_stack(config, ArtifactStore(tmp)).statuses()
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            return [Check("providers", WARN, f"could not evaluate provider readiness ({e})")]
    out = []
    for st in statuses:
        name = f"provider:{st.kind}"
        text = f"{st.name}: {st.message}".strip()
        if st.state in (READY, FAKE):
            out.append(Check(name, PASS, f"{text} ({st.state})"))
        elif st.state == DISABLED:
            out.append(Check(name, INFO, text, "Optional: select a provider with STORYFLOW_* settings (see docs)."))
        elif st.state in (UNAVAILABLE, MISCONFIGURED):
            out.append(Check(name, WARN, f"{text} ({st.state})", "Fix the STORYFLOW_* configuration for this provider."))
        else:
            out.append(Check(name, INFO, f"{text} ({st.state})"))
    return out


def run_doctor(*, db_path: Path | str | None = None, artifact_root: Path | str | None = None,
               frontend_dir: Path | str | None = None, port: int = DEFAULT_PORT, log_dir: Path | str | None = None,
               quick: bool = False, provider_env: dict | None = None,
               bootstrap_script: Path | str | None = PROJECT_ROOT / "bootstrap.py") -> DoctorReport:
    """Run every check and return a report. ``provider_env`` (a dict) replaces os.environ/.env for the
    provider check (tests); None uses the real environment."""
    db_path = Path(db_path) if db_path else default_db_path()
    artifact_root = Path(artifact_root) if artifact_root else default_artifact_root()
    frontend_dir = Path(frontend_dir) if frontend_dir else PROJECT_ROOT / "frontend"
    log_dir = Path(log_dir) if log_dir else RUNTIME_DIR / "logs"
    rep = DoctorReport()
    add = rep.checks.append

    v = sys.version_info
    ver = f"{v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < MIN_PYTHON:
        add(Check("python", FAIL, f"Python {ver} is too old (need >= 3.11)", "Install Python 3.13 and recreate backend/.venv."))
    elif (v.major, v.minor) != TESTED_PYTHON:
        add(Check("python", WARN, f"Python {ver} works but 3.13 is the tested version"))
    else:
        add(Check("python", PASS, f"Python {ver}"))

    missing = [p for p in REQUIRED_PACKAGES if importlib.util.find_spec(p) is None]
    if missing:
        add(Check("packages", FAIL, f"missing Python packages: {', '.join(missing)}",
                  "Run: pip install -r backend/requirements.txt (inside backend/.venv)."))
    else:
        add(Check("packages", PASS, "required packages are importable: " + ", ".join(REQUIRED_PACKAGES)))

    if quick:
        add(Check("bootstrap", INFO, "skipped (--quick)"))
    else:
        add(_bootstrap_check(Path(bootstrap_script) if bootstrap_script else None))

    rep.checks.extend(_db_checks(db_path, quick))
    add(_check_dir_writable("artifact_dir", artifact_root, "artifact directory"))

    try:
        free = shutil.disk_usage(_nearest_existing(artifact_root)).free
    except OSError as e:
        add(Check("disk_space", WARN, f"cannot read free disk space ({e})"))
    else:
        if free < MIN_FREE_BYTES:
            add(Check("disk_space", WARN, f"only {free / (1 << 20):.0f} MB free (< 1 GB)",
                      "Free disk space; audio artifacts and backups need room."))
        else:
            add(Check("disk_space", PASS, f"{free / (1 << 30):.1f} GB free"))

    index = frontend_dir / "dist" / "index.html"
    if index.is_file():
        add(Check("frontend", PASS, f"frontend build present ({index})"))
    else:
        add(Check("frontend", WARN, f"frontend build not found at {index}",
                  "Build it: cd frontend && npm install && npm run build  (the API still works without it)."))

    add(_port_free(port))
    rep.checks.extend(_provider_checks(provider_env, artifact_root))
    add(_check_dir_writable("log_dir", log_dir, "log directory"))
    return rep
