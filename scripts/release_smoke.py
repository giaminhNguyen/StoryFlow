#!/usr/bin/env python
"""StoryFlow release smoke: the final regression matrix as one runnable script.

Python standard library only; the project's own tools are run as subprocesses with the
``backend/.venv`` interpreter.  Everything that needs data runs against a CLEAN TEMPORARY
workspace (database, artifacts, logs, backups); the real ``runtime/`` directory is never touched.

    python scripts/release_smoke.py                       # every step
    python scripts/release_smoke.py --skip-backend-tests  # faster (backend suite run separately)
    python scripts/release_smoke.py --skip-frontend       # no npm; the server runs with --no-frontend
    python scripts/release_smoke.py --only 5,6,shutdown   # by number or id
    python scripts/release_smoke.py --list
    python scripts/release_smoke.py --json                # machine-readable result on stdout

Exit code 0 only if every executed step passed (skipped steps do not count as failures).
Step 12 (real providers) runs only with STORYFLOW_RUN_REAL_SMOKE=1.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
IS_WIN = os.name == "nt"
VENV_PY = BACKEND / ".venv" / ("Scripts/python.exe" if IS_WIN else "bin/python")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
DEMO_VIDEO = "demo-video"
SHUTDOWN_RE = re.compile(r"shutdown complete", re.I)


class StepFailure(Exception):
    """A step's assertion failed; the message is the human-readable reason."""


class StepSkipped(Exception):
    """A step decided it does not apply (reason in the message)."""


# ------------------------------------------------------------------------------ pure helpers


def free_port() -> int:
    """Ask the OS for a currently free loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def python_exe() -> str:
    """The backend venv interpreter (falls back to the current one with a warning at run time)."""
    return str(VENV_PY) if VENV_PY.exists() else sys.executable


def sqlite_url(path: Path) -> str:
    return "sqlite:///" + Path(path).as_posix()


def build_server_cmd(python: str, port: int, db: Path, artifacts: Path, logs: Path,
                     frontend_dir: Path | None, fake: bool = True) -> list[str]:
    cmd = [python, "-m", "storyflow.api"]
    if fake:
        cmd.append("--fake")
    cmd += ["--port", str(port), "--database-url", sqlite_url(db), "--artifact-root", str(artifacts),
            "--log-dir", str(logs)]
    cmd += ["--frontend-dir", str(frontend_dir)] if frontend_dir is not None else ["--no-frontend"]
    return cmd


def build_cli_cmd(python: str, *args: str) -> list[str]:
    return [python, "-m", "storyflow", *args]


def format_result(r: "Result") -> str:
    line = f"[{r.status}] {r.number:>2}. {r.name} ({r.seconds:.1f}s)"
    return f"{line} - {r.detail}" if r.detail else line


def format_summary(results: list["Result"]) -> str:
    width = max([len(r.name) for r in results] + [4])
    rows = ["", "=" * (width + 30), f"{'#':>2}  {'step':<{width}}  {'result':<6}  seconds", "-" * (width + 30)]
    for r in results:
        rows.append(f"{r.number:>2}  {r.name:<{width}}  {r.status:<6}  {r.seconds:7.1f}")
    failed = [r for r in results if r.status == FAIL]
    passed = sum(1 for r in results if r.status == PASS)
    skipped = sum(1 for r in results if r.status == SKIP)
    rows.append("-" * (width + 30))
    rows.append(f"{passed} passed, {len(failed)} failed, {skipped} skipped -> "
                + ("RELEASE SMOKE PASS" if not failed else "RELEASE SMOKE FAIL"))
    return "\n".join(rows)


@dataclass
class Result:
    number: int
    id: str
    name: str
    status: str
    seconds: float
    detail: str = ""


@dataclass
class Step:
    number: int
    id: str
    name: str
    fn: Callable[["Ctx"], str]
    needs_frontend: bool = False
    slow_backend_tests: bool = False


def parse_only(text: str | None, steps: list[Step]) -> list[str]:
    """Turn ``5,6,shutdown`` (numbers or ids, comma/space separated) into step ids. Raises ValueError."""
    if not text:
        return []
    by_num = {str(s.number): s.id for s in steps}
    ids = {s.id for s in steps}
    out = []
    for tok in re.split(r"[,\s]+", text.strip()):
        if not tok:
            continue
        if tok in by_num:
            out.append(by_num[tok])
        elif tok in ids:
            out.append(tok)
        else:
            raise ValueError(f"unknown step {tok!r}; use --list")
    return out


def select_steps(steps: list[Step], only: list[str], skip_frontend: bool, skip_backend_tests: bool
                 ) -> list[tuple[Step, str | None]]:
    """Returns (step, skip_reason|None) in registry order."""
    chosen = []
    for s in steps:
        if only and s.id not in only:
            continue
        reason = None
        if skip_frontend and s.needs_frontend:
            reason = "--skip-frontend"
        elif skip_backend_tests and s.slow_backend_tests:
            reason = "--skip-backend-tests"
        chosen.append((s, reason))
    return chosen


# ------------------------------------------------------------------------------ process helpers


def kill_tree(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        if IS_WIN:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=10)
    except Exception:
        pass


def clean_env(extra: dict | None = None) -> dict:
    """Environment without operator provider configuration (fakes are selected explicitly)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("STORYFLOW_")}
    env["PYTHONPATH"] = str(BACKEND)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def run(cmd: list[str], cwd: Path, timeout: float, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, env=env if env is not None else clean_env())


def tail(text: str, n: int = 12) -> str:
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    return " | ".join(lines[-n:])[-900:]


def npm_cmd() -> str:
    found = shutil.which("npm.cmd" if IS_WIN else "npm") or shutil.which("npm")
    if not found:
        raise StepFailure("npm was not found on PATH (Node.js >= 20 is required)")
    return found


# ------------------------------------------------------------------------------ HTTP helpers


class Http:
    def __init__(self, port: int):
        self.port = port

    def request(self, method: str, path: str, body=None, headers=None, timeout: float = 30.0):
        """Raw request (the path is sent exactly as given). Returns (status, headers, bytes)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            hdrs = dict(headers or {})
            data = None
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                hdrs.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, resp.read()
        finally:
            conn.close()

    def json(self, method: str, path: str, body=None, expect: tuple[int, ...] = (200, 201)):
        status, _h, raw = self.request(method, path, body)
        try:
            data = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            data = None
        if expect and status not in expect:
            raise StepFailure(f"{method} {path} -> HTTP {status}, expected {expect}: {raw[:200]!r}")
        return status, data


class Server:
    """The real ``python -m storyflow.api`` subprocess on a temporary workspace."""

    def __init__(self, ws: Path, frontend_dir: Path | None, name: str = "server"):
        self.ws = ws
        self.db = ws / "storyflow.db"
        self.artifacts = ws / "artifacts"
        self.logs = ws / "logs"
        self.console = ws / f"{name}.console.log"
        self.frontend_dir = frontend_dir
        self.port = 0
        self.proc: subprocess.Popen | None = None
        self.http: Http | None = None
        self._fh = None

    def start(self, timeout: float = 90.0) -> "Server":
        self.ws.mkdir(parents=True, exist_ok=True)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        cmd = build_server_cmd(python_exe(), self.port, self.db, self.artifacts, self.logs, self.frontend_dir)
        self._fh = open(self.console, "ab")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
        self.proc = subprocess.Popen(cmd, cwd=str(BACKEND), stdout=self._fh, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, env=clean_env(), creationflags=flags,
                                     start_new_session=not IS_WIN)
        self.http = Http(self.port)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise StepFailure(f"server exited early (code {self.proc.returncode}): {tail(self.output())}")
            try:
                status, _h, _b = self.http.request("GET", "/api/health", timeout=3)
                if status == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.25)
        raise StepFailure(f"server did not become healthy within {timeout:.0f}s: {tail(self.output())}")

    def output(self) -> str:
        parts = []
        try:
            if self._fh:
                self._fh.flush()
            parts.append(self.console.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
        if self.logs.exists():
            for f in sorted(self.logs.glob("*")):
                try:
                    if f.is_file():
                        parts.append(f.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    pass
        return "\n".join(parts)

    def stop_graceful(self, timeout: float = 30.0) -> int:
        """CTRL_BREAK (Windows, new process group) / SIGINT (POSIX); returns the exit code."""
        if self.proc is None:
            raise StepFailure("server was never started")
        if self.proc.poll() is None:
            try:
                if IS_WIN:
                    os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
                else:
                    self.proc.send_signal(signal.SIGINT)
            except OSError as exc:
                raise StepFailure(f"could not signal the server: {exc}") from exc
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(self.proc)
            raise StepFailure(f"server did not stop within {timeout:.0f}s after the shutdown signal") from None
        return code

    def close(self) -> None:
        kill_tree(self.proc)
        if self._fh:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None


# ------------------------------------------------------------------------------ API scenario helpers


def wait_for(fn: Callable[[], object], timeout: float, what: str, interval: float = 0.4):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise StepFailure(f"timed out after {timeout:.0f}s waiting for {what}")


def create_workflow(h: Http, name: str, video_id: str = DEMO_VIDEO) -> str:
    _s, data = h.json("POST", "/api/workflows", {
        "name": name, "config": {"source": {"video_id": video_id, "languages": ["en"]}},
        "client_key": f"smoke-{uuid.uuid4().hex[:12]}"})
    return data["workflow"]["id"]


def add_project(h: Http, wid: str, title: str) -> str:
    _s, data = h.json("POST", f"/api/workflows/{wid}/projects", {"title": title})
    return data["project"]["id"]


def assign_runner(h: Http, wid: str) -> str:
    """Assign the (single) discovered runner to ``wid``, unassigning it from an earlier workflow."""
    def runners():
        _s, d = h.json("GET", "/api/runners")
        return d["runners"]
    rs = wait_for(runners, 45, "a discovered runner")
    r = rs[0]
    if r.get("assigned"):
        h.json("POST", f"/api/runners/{r['id']}/unassign", None, expect=(200,))
    h.json("POST", f"/api/runners/{r['id']}/assign", {"workflow_id": wid}, expect=(200,))
    return r["id"]


def get_workflow(h: Http, wid: str) -> dict:
    return h.json("GET", f"/api/workflows/{wid}")[1]


def wait_status(h: Http, wid: str, wanted: set[str], timeout: float = 120.0) -> dict:
    seen: dict = {}

    def poll():
        wf = get_workflow(h, wid)
        seen["s"] = f"{wf['status']}/{wf.get('status_reason')}"
        return wf if wf["status"] in wanted else None
    try:
        return wait_for(poll, timeout, f"workflow {wid} to reach {sorted(wanted)}", interval=0.3)
    except StepFailure as exc:
        raise StepFailure(f"{exc} (last seen {seen.get('s')})") from None


def expect_error(h: Http, method: str, path: str, status: int, codes: set[str], body=None) -> dict:
    st, _h, raw = h.request(method, path, body)
    if st != status:
        raise StepFailure(f"{method} {path} -> HTTP {st}, expected {status}: {raw[:160]!r}")
    err = (json.loads(raw.decode("utf-8")) or {}).get("error", {})
    if codes and err.get("code") not in codes:
        raise StepFailure(f"{method} {path} -> error code {err.get('code')!r}, expected one of {sorted(codes)}")
    return err


def artifact_url(rel: str) -> str:
    return "/api/artifacts/" + quote(rel, safe="/")


def run_completed_flow(h: Http, name: str = "smoke flow") -> dict:
    wid = create_workflow(h, name)
    pid = add_project(h, wid, name + " project")
    assign_runner(h, wid)
    h.json("POST", f"/api/workflows/{wid}/start", expect=(200,))
    wf = wait_status(h, wid, {"finished", "cancelled", "paused"}, 150)
    if wf["status"] != "finished":
        raise StepFailure(f"workflow ended as {wf['status']}/{wf.get('status_reason')}, expected completed")
    return {"workflow_id": wid, "project_id": pid}


def check_outputs(h: Http, pid: str) -> str:
    proj = h.json("GET", f"/api/projects/{pid}")[1]
    sv = proj.get("story_version") or {}
    if not sv.get("content_path"):
        raise StepFailure("completed project has no story version")
    st, hd, body = h.request("GET", artifact_url(sv["content_path"]))
    if st != 200 or not body.strip():
        raise StepFailure(f"story artifact -> HTTP {st}, {len(body)} bytes")
    chunks = (proj.get("audio") or {}).get("chunks") or []
    if not chunks:
        raise StepFailure("completed project has no audio chunks")
    st, hd, body = h.request("GET", artifact_url(chunks[0]["artifact_path"]))
    if st != 200 or "audio/wav" not in hd.get("content-type", "") or body[:4] != b"RIFF":
        raise StepFailure(f"audio chunk -> HTTP {st}, type {hd.get('content-type')}, head {body[:4]!r}")
    return f"story {len(sv.get('content_path', ''))}c path, {len(chunks)} audio chunk(s)"


# ------------------------------------------------------------------------------ context


class Ctx:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.tmp = Path(tempfile.mkdtemp(prefix="storyflow-smoke-"))
        self.servers: list[Server] = []
        self.cache: dict = {}
        self._n = 0

    @property
    def frontend_dir(self) -> Path | None:
        return None if self.args.skip_frontend else FRONTEND / "dist"

    def workspace(self, name: str) -> Path:
        self._n += 1
        p = self.tmp / f"{self._n:02d}-{name}"
        p.mkdir(parents=True, exist_ok=True)
        return p

    def new_server(self, name: str) -> Server:
        s = Server(self.workspace(name), self.frontend_dir, name)
        self.servers.append(s)
        return s.start()

    def shared(self) -> tuple[Server, dict]:
        """One server + one completed workflow, reused by steps 5, 6, 9 and 10."""
        if "shared_error" in self.cache:
            raise StepFailure("shared server/workflow setup failed earlier: " + self.cache["shared_error"])
        if "shared" not in self.cache:
            try:
                srv = self.new_server("shared")
                self.cache["shared"] = (srv, run_completed_flow(srv.http, "shared flow"))
            except StepFailure as exc:
                self.cache["shared_error"] = str(exc)
                raise
        return self.cache["shared"]

    def cleanup(self) -> None:
        for s in self.servers:
            s.close()
        for _ in range(10):
            try:
                shutil.rmtree(self.tmp)
                break
            except OSError:
                time.sleep(0.3)


def cli_target(ws: Path, sub: str = "backup") -> list[str]:
    """Flags pointing a ``python -m storyflow <sub>`` command at a temporary workspace (never runtime/)."""
    db = ["--db", str(ws / "storyflow.db")]
    if sub == "migrate":  # migrate has no artifacts; keep its automatic backups inside the temp workspace
        return db + ["--backup-dir", str(ws / "backups")]
    return db + ["--artifact-root", str(ws / "artifacts")]


def cli(ctx: Ctx, *args: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
    return run(build_cli_cmd(python_exe(), *args), BACKEND, timeout)


# ------------------------------------------------------------------------------ steps


def step_bootstrap(ctx: Ctx) -> str:
    p = run([sys.executable, str(ROOT / "bootstrap.py"), "--check"], ROOT, 120)
    if p.returncode != 0:
        raise StepFailure("bootstrap.py --check failed (run scripts\\setup.bat): " + tail(p.stdout + p.stderr))
    return "pinned sources match sources.lock.json"


def head_revision() -> str:
    p = run([python_exe(), "-c",
             "from alembic.script import ScriptDirectory;from storyflow.runtime.app import alembic_config;"
             "print(ScriptDirectory.from_config(alembic_config('sqlite://')).get_current_head())"], BACKEND, 60)
    if p.returncode != 0:
        raise StepFailure("cannot determine the migration head: " + tail(p.stderr))
    return p.stdout.strip().splitlines()[-1]


def step_migrate(ctx: Ctx) -> str:
    ws = ctx.workspace("migrate")
    db = ws / "storyflow.db"
    tgt = cli_target(ws, "migrate")
    dry = cli(ctx, "migrate", "--dry-run", *tgt)
    if dry.returncode != 0:
        raise StepFailure("migrate --dry-run on a fresh database failed: " + tail(dry.stdout + dry.stderr))
    if db.exists() and db.stat().st_size > 0:
        raise StepFailure("migrate --dry-run modified the database")
    real = cli(ctx, "migrate", *tgt)
    if real.returncode != 0:
        raise StepFailure("migrate on a fresh database failed: " + tail(real.stdout + real.stderr))
    head = head_revision()
    con = sqlite3.connect(str(db))
    try:
        rev = con.execute("select version_num from alembic_version").fetchone()
    finally:
        con.close()
    if not rev or rev[0] != head:
        raise StepFailure(f"database revision {rev} != head {head}")
    again = cli(ctx, "migrate", "--dry-run", *tgt)
    if again.returncode != 0:
        raise StepFailure("migrate --dry-run at head failed: " + tail(again.stdout + again.stderr))
    return f"empty -> {head}; dry-run leaves the database untouched; idempotent at head"


def step_backend_tests(ctx: Ctx) -> str:
    p = run([python_exe(), "-m", "pytest", "-q", "-p", "no:cacheprovider"], BACKEND, 3600)
    last = (p.stdout.strip().splitlines() or [""])[-1]
    if p.returncode != 0:
        raise StepFailure("backend pytest failed: " + tail(p.stdout))
    return last


def step_frontend(ctx: Ctx) -> str:
    npm = npm_cmd()
    out = []
    for label, args in (("build (typecheck+vite)", ["run", "build"]), ("unit tests", ["test"])):
        p = run([npm, *args], FRONTEND, 900)
        if p.returncode != 0:
            raise StepFailure(f"npm {' '.join(args)} failed: " + tail(p.stdout + p.stderr))
        out.append(label + " ok")
    if not (FRONTEND / "dist" / "index.html").exists():
        raise StepFailure("frontend/dist/index.html missing after the build")
    return "; ".join(out)


def step_e2e(ctx: Ctx) -> str:
    srv, flow = ctx.shared()
    h = srv.http
    if ctx.frontend_dir is not None:
        st, hd, body = h.request("GET", "/")
        if st != 200 or "text/html" not in hd.get("content-type", "") or b"<div id=\"root\"" not in body:
            raise StepFailure(f"GET / -> HTTP {st} {hd.get('content-type')} (built UI not served)")
        ui = "UI served at /"
    else:
        ui = "UI skipped"
    health = h.json("GET", "/api/health")[1]
    if health.get("status") != "ok":
        raise StepFailure(f"/api/health status {health.get('status')}")
    return f"{ui}; workflow completed; {check_outputs(h, flow['project_id'])}"


def step_lifecycle(ctx: Ctx) -> str:
    srv, _flow = ctx.shared()
    h = srv.http
    notes = []
    # cancel on a draft workflow: terminal, then further commands are refused
    wid = create_workflow(h, "cancel me")
    add_project(h, wid, "p")
    expect_error(h, "POST", f"/api/workflows/{wid}/pause", 409, {"invalid_state"})       # not started
    h.json("POST", f"/api/workflows/{wid}/cancel", expect=(200,))
    if get_workflow(h, wid)["status"] != "cancelled":
        raise StepFailure("cancel did not reach status cancelled")
    h.json("POST", f"/api/workflows/{wid}/cancel", expect=(200,))                         # idempotent
    expect_error(h, "POST", f"/api/workflows/{wid}/start", 409, {"invalid_state"})
    expect_error(h, "POST", f"/api/workflows/{wid}/retry", 409, {"invalid_state", "not_retryable"})
    notes.append("cancel terminal+idempotent")

    # pause / resume on a running workflow
    wid = create_workflow(h, "pause me")
    add_project(h, wid, "p")
    assign_runner(h, wid)
    h.json("POST", f"/api/workflows/{wid}/start", expect=(200,))
    st, _h, raw = h.request("POST", f"/api/workflows/{wid}/pause")
    if st == 200:
        wf = get_workflow(h, wid)
        if wf["status"] == "paused":
            if wf.get("status_reason") != "operator":
                raise StepFailure(f"operator pause has reason {wf.get('status_reason')!r}")
            h.json("POST", f"/api/workflows/{wid}/pause", expect=(200,))                  # idempotent
            expect_error(h, "POST", f"/api/workflows/{wid}/retry", 409, {"not_retryable"})
            h.json("POST", f"/api/workflows/{wid}/resume", expect=(200,))
            notes.append("pause/resume + retry-while-paused not_retryable")
        else:
            notes.append(f"pause raced with completion ({wf['status']})")
    elif st == 409:
        notes.append("pause raced with completion (409)")
    else:
        raise StepFailure(f"pause -> HTTP {st}: {raw[:160]!r}")
    wf = wait_status(h, wid, {"finished", "paused"}, 150)
    if wf["status"] != "finished":
        raise StepFailure(f"resumed workflow ended {wf['status']}")
    expect_error(h, "POST", f"/api/workflows/{wid}/retry", 409, {"not_retryable", "invalid_state"})

    # failing workflow: unknown source video -> the source step fails permanently
    wid = create_workflow(h, "will fail", video_id="no-such-video")
    pid = add_project(h, wid, "p")
    assign_runner(h, wid)
    h.json("POST", f"/api/workflows/{wid}/start", expect=(200,))
    wf = wait_status(h, wid, {"paused", "finished"}, 120)
    if wf["status"] != "paused" or wf.get("status_reason") != "step_failed":
        raise StepFailure(f"bad-source workflow is {wf['status']}/{wf.get('status_reason')}, expected paused/step_failed")
    expect_error(h, "POST", f"/api/workflows/{wid}/resume", 409, {"invalid_state"})       # must use retry
    expect_error(h, "POST", f"/api/workflows/{wid}/retry", 404, {"not_found"}, {"project_id": "nope"})
    # A source-step failure leaves no failed job, so the per-project retry is refused as not_retryable
    # (project_not_failed) while the workflow-level retry re-activates it; both are accepted API semantics.
    st, _hd, raw = h.request("POST", f"/api/workflows/{wid}/retry", {"project_id": pid})
    if st == 409:
        expect_error(h, "POST", f"/api/workflows/{wid}/retry", 409, {"not_retryable"}, {"project_id": pid})
        per_project = "per-project retry not_retryable"
        # KNOWN QUIRK (reported): the refused per-project retry may flip the workflow to active for a moment
        # before the source step fails again; wait for it to settle back to paused/step_failed.
        wait_status(h, wid, {"paused"}, 60)
        h.json("POST", f"/api/workflows/{wid}/retry", {}, expect=(200,))
    elif st == 200:
        per_project = "per-project retry accepted"
    else:
        raise StepFailure(f"per-project retry -> HTTP {st}: {raw[:160]!r}")
    wf = wait_status(h, wid, {"paused", "finished"}, 120)
    if wf["status"] != "paused" or wf.get("status_reason") != "step_failed":
        raise StepFailure(f"retried bad-source workflow became {wf['status']}/{wf.get('status_reason')}")
    notes.append(f"failed step: resume refused, {per_project}, workflow retry re-runs and fails again")
    h.json("POST", f"/api/workflows/{wid}/cancel", expect=(200,))
    if get_workflow(h, wid)["status"] != "cancelled":
        raise StepFailure("cancel of a failed workflow did not reach cancelled")
    expect_error(h, "POST", f"/api/workflows/{wid}/retry", 409, {"invalid_state", "not_retryable"})
    notes.append("cancel after failure terminal")
    return "; ".join(notes)


DEMO_CHANNEL = "https://www.youtube.com/@demo"


def step_batch(ctx: Ctx) -> str:
    """A whole channel through the real server (offline demo channel): one video has no subtitle and is skipped
    while the rest finish (window of 2, quality preset = review + revision), each completed project ends with ONE
    joined audio file, a skipped project can be retried, and a re-scan adds nothing new."""
    srv, _flow = ctx.shared()
    h = srv.http
    notes = []
    _s, data = h.json("POST", "/api/workflows", {
        "name": "batch smoke", "client_key": f"smoke-{uuid.uuid4().hex[:12]}",
        "config": {"source": {"languages": ["en"]}, "preset": "quality", "batch": {"max_active": 2},
                   "failure_policy": {"on_no_subtitle": "skip", "on_permanent_error": "continue"}}})
    wid = data["workflow"]["id"]
    cfg_seen = get_workflow(h, wid)
    if cfg_seen.get("preset") != "quality":
        raise StepFailure(f"workflow preset is {cfg_seen.get('preset')!r}, expected quality")

    expect_error(h, "POST", f"/api/workflows/{wid}/sources", 422, {"validation"}, {"sources": ["https://example.com/x"]})
    _s, res = h.json("POST", f"/api/workflows/{wid}/sources", {
        "sources": [DEMO_CHANNEL, "https://youtu.be/demoVideo01"], "limit": 10}, expect=(201,))
    result = res["result"]
    if len(result["added"]) != 4 or [d["reason"] for d in result["duplicates"]] != ["repeated"]:
        raise StepFailure(f"sources: added {len(result['added'])}, duplicates {result['duplicates']}")
    _s, feeds = h.json("GET", f"/api/workflows/{wid}/feeds")
    if [(f["kind"], f["known_count"]) for f in feeds["feeds"]] != [("channel", 4)]:
        raise StepFailure(f"feeds: {feeds['feeds']}")
    notes.append("channel expanded to 4 projects, repeated link deduplicated")

    assign_runner(h, wid)
    h.json("POST", f"/api/workflows/{wid}/start", expect=(200,))
    wf = wait_status(h, wid, {"finished", "paused"}, 240)
    if wf["status"] != "finished":
        raise StepFailure(f"batch ended as {wf['status']}/{wf.get('status_reason')} (a skipped video must not pause it)")
    counts = wf["counts"]
    if (counts["completed"], counts["skipped"], counts["failed"]) != (3, 1, 0):
        raise StepFailure(f"batch counts {counts}")
    skipped = [p for p in wf["projects"] if p["state"] == "skipped"]
    if len(skipped) != 1 or skipped[0]["status_reason"] != "subtitles_unavailable":
        raise StepFailure(f"skipped projects: {[(p['title'], p['status_reason']) for p in skipped]}")
    notes.append("3 completed + 1 skipped, workflow finished")

    for p in wf["projects"]:
        if p["state"] != "completed":
            continue
        proj = h.json("GET", f"/api/projects/{p['id']}")[1]
        final = (proj.get("audio") or {}).get("final_path")
        if not final:
            raise StepFailure(f"project {p['title']} has no final audio path")
        st, hd, body = h.request("GET", artifact_url(final))
        if st != 200 or body[:4] != b"RIFF" or "audio/wav" not in hd.get("content-type", ""):
            raise StepFailure(f"final audio -> HTTP {st}, head {body[:4]!r}")
        review = proj.get("review") or {}
        if review.get("status") != "completed" or review.get("verdict") not in ("approve", "revise"):
            raise StepFailure(f"quality preset produced no completed review: {review}")
    notes.append("every completed project has review + one joined final.wav")

    st_id = skipped[0]["id"]
    expect_error(h, "POST", f"/api/projects/{[p for p in wf['projects'] if p['state'] == 'completed'][0]['id']}/retry",
                 409, {"not_retryable"})
    h.json("POST", f"/api/projects/{st_id}/retry", expect=(200,))
    wf = wait_status(h, wid, {"finished", "paused"}, 120)
    again = [p for p in wf["projects"] if p["id"] == st_id][0]
    if wf["status"] != "finished" or again["state"] != "skipped":
        raise StepFailure(f"retried skipped project is {again['state']} in a {wf['status']} workflow")
    notes.append("retry of the skipped project re-opens and re-finishes")

    _s, sync = h.json("POST", f"/api/workflows/{wid}/sync", expect=(200,))
    if sync["result"]["added"] or sync["result"]["errors"]:
        raise StepFailure(f"a re-scan of an unchanged channel added/failed: {sync['result']}")
    notes.append("re-scan adds nothing")
    return "; ".join(notes)


def _project_integrity(h: Http, pid: str) -> dict:
    proj = h.json("GET", f"/api/projects/{pid}")[1]
    idx = [c["chunk_index"] for c in (proj.get("audio") or {}).get("chunks") or []]
    if len(idx) != len(set(idx)):
        raise StepFailure(f"duplicate audio chunk indexes in project {pid}: {idx}")
    audio = proj.get("audio") or {}
    if audio.get("chunk_count") is not None and audio.get("registered_chunks") not in (None, audio["chunk_count"]):
        raise StepFailure(f"project {pid}: registered_chunks {audio.get('registered_chunks')} != {audio['chunk_count']}")
    steps = [s["step"] for s in proj.get("steps") or []]
    if len(steps) != len(set(steps)):
        raise StepFailure(f"duplicate pipeline steps in project {pid}: {steps}")
    return proj


def step_restart(ctx: Ctx) -> str:
    srv = ctx.new_server("restart")
    h = srv.http
    done = run_completed_flow(h, "before restart")
    before_runners = len(h.json("GET", "/api/runners")[1]["runners"])
    wid = create_workflow(h, "interrupted")
    pid = add_project(h, wid, "interrupted project")
    assign_runner(h, wid)
    h.json("POST", f"/api/workflows/{wid}/start", expect=(200,))
    wait_for(lambda: get_workflow(h, wid)["status"] in ("active", "finished"), 30, "the workflow to be active", 0.05)
    finished_early = get_workflow(h, wid)["status"] == "finished"
    code = srv.stop_graceful(30)
    if code != 0:
        raise StepFailure(f"first server exit code {code}")
    srv.close()
    srv.start()
    h = srv.http
    wf = wait_status(h, wid, {"finished", "cancelled", "paused"}, 150)
    if wf["status"] != "finished":
        raise StepFailure(f"interrupted workflow ended {wf['status']}/{wf.get('status_reason')} after restart")
    _project_integrity(h, pid)
    _project_integrity(h, done["project_id"])
    check_outputs(h, pid)
    wfs = h.json("GET", "/api/workflows")[1]["workflows"]
    ids = [w["id"] for w in wfs]
    if len(ids) != len(set(ids)) or len(ids) != 2:
        raise StepFailure(f"workflow list after restart has {len(ids)} rows ({len(set(ids))} distinct), expected 2")
    if len(get_workflow(h, wid)["projects"]) != 1:
        raise StepFailure("interrupted workflow has a duplicated project after restart")
    if len(h.json("GET", "/api/runners")[1]["runners"]) != before_runners:
        raise StepFailure("runner rows changed across restart (duplicate discovery)")
    return "restart resumed" + (" (pipeline had already finished before the stop)" if finished_early else " mid-pipeline") \
        + "; 2 workflows, no duplicate rows"


TWO_RUNTIME_TESTS = [
    "tests/test_phase5_integration.py::test_two_runtime_instances_never_duplicate_work",
    "tests/test_runtime.py::test_two_independent_runtimes_same_db_no_duplicates",
    "tests/test_runtime.py::test_full_fake_pipeline_via_run_forever_and_session_isolation",
]


def step_isolation(ctx: Ctx) -> str:
    p = run([python_exe(), "-m", "pytest", "-q", "-p", "no:cacheprovider", *TWO_RUNTIME_TESTS], BACKEND, 900)
    if p.returncode != 0:
        raise StepFailure("two-runtime/session-isolation tests failed: " + tail(p.stdout))
    return (p.stdout.strip().splitlines() or [""])[-1]


def step_artifacts(ctx: Ctx) -> str:
    srv, flow = ctx.shared()
    h = srv.http
    proj = h.json("GET", f"/api/projects/{flow['project_id']}")[1]
    real = proj["story_version"]["content_path"]
    st, _hd, _b = h.request("GET", artifact_url(real))
    if st != 200:
        raise StepFailure(f"real artifact -> HTTP {st}")
    pid = flow["project_id"]
    bad = [
        "/api/artifacts/projects/nope/none.wav",
        "/api/artifacts/projects/%2e%2e/%2e%2e/storyflow.db",
        "/api/artifacts/projects/../../storyflow.db",
        "/api/artifacts/..%5c..%5cstoryflow.db",
        f"/api/artifacts/projects/{pid}/../../../storyflow.db",
        f"/api/artifacts/projects/{pid}/%2e%2e%2f%2e%2e%2fstoryflow.db",
        "/api/artifacts/%2fWindows/win.ini",
        "/api/artifacts/C:/Windows/win.ini",
        "/api/artifacts/projects/" + "a" * 5000,
    ]
    bodies = set()
    for path in bad:
        try:
            st, _hd, body = h.request("GET", path)
        except (OSError, http.client.HTTPException) as exc:
            raise StepFailure(f"{path[:60]} -> connection error {exc}") from exc
        if st != 404:
            raise StepFailure(f"{path[:70]} -> HTTP {st}, expected the uniform 404")
        bodies.add(body)
    if len(bodies) != 1:
        raise StepFailure("404 bodies differ between refusals (the store layout could be probed)")
    return f"real artifact 200; {len(bad)} traversal/absolute/unknown URLs -> identical 404"


def step_backup(ctx: Ctx) -> str:
    srv, flow = ctx.shared()  # server keeps running during the backup
    h = srv.http
    wfs_before = sorted(w["id"] for w in h.json("GET", "/api/workflows")[1]["workflows"])
    dest = ctx.workspace("backups")
    p = cli(ctx, "backup", "--to", str(dest / "b1"), "--label", "smoke", "--json", *cli_target(srv.ws))
    if p.returncode != 0:
        raise StepFailure("backup failed: " + tail(p.stdout + p.stderr))
    made = [d for d in dest.iterdir() if d.is_dir() and (d / "manifest.json").exists()]
    if len(made) != 1:
        raise StepFailure(f"expected exactly one backup with a manifest.json in {dest.name}, found {len(made)}")
    manifest = json.loads((made[0] / "manifest.json").read_text(encoding="utf-8"))
    if h.json("GET", "/api/health")[1].get("status") != "ok":
        raise StepFailure("server unhealthy after the online backup")
    restored = ctx.workspace("restored")
    p = cli(ctx, "restore", "--from", str(made[0]), *cli_target(restored))
    if p.returncode != 0:
        raise StepFailure("restore failed: " + tail(p.stdout + p.stderr))
    p = cli(ctx, "doctor", "--json", *cli_target(restored), "--log-dir", str(restored / "logs"),
            *(["--frontend-dir", str(ctx.frontend_dir)] if ctx.frontend_dir else []))
    doc = tail(p.stdout + p.stderr, 3)
    if p.returncode == 2:
        raise StepFailure("doctor usage error on the restored copy: " + doc)
    second = Server(restored, ctx.frontend_dir, "restored")
    ctx.servers.append(second)
    second.start()
    after = sorted(w["id"] for w in second.http.json("GET", "/api/workflows")[1]["workflows"])
    if after != wfs_before:
        raise StepFailure(f"restored workflow list differs: {after} != {wfs_before}")
    check_outputs(second.http, flow["project_id"])
    return f"online backup ok; restored copy serves the same {len(after)} workflow(s) and artifacts; doctor exit {p.returncode}"


def step_shutdown(ctx: Ctx) -> str:
    srv = ctx.new_server("shutdown")
    t0 = time.time()
    code = srv.stop_graceful(30)
    took = time.time() - t0
    if code != 0:
        raise StepFailure(f"exit code {code} after the shutdown signal, expected 0: {tail(srv.output())}")
    out = srv.output()
    if not SHUTDOWN_RE.search(out):
        raise StepFailure("no shutdown line in the server output/log: " + tail(out))
    return f"exit 0 in {took:.1f}s with a shutdown log line"


def step_real(ctx: Ctx) -> str:
    if os.environ.get("STORYFLOW_RUN_REAL_SMOKE") != "1":
        raise StepSkipped("set STORYFLOW_RUN_REAL_SMOKE=1 (plus provider config) to run the real-provider smokes")
    env = {k: v for k, v in os.environ.items()}
    env["PYTHONPATH"] = str(BACKEND)
    p = run([python_exe(), "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/smoke_real", "-rs"], BACKEND, 3600, env)
    if p.returncode != 0:
        raise StepFailure("real-provider smoke failed: " + tail(p.stdout))
    return (p.stdout.strip().splitlines() or [""])[-1]


STEPS: list[Step] = [
    Step(1, "bootstrap", "bootstrap pins (--check)", step_bootstrap),
    Step(2, "migrate", "migration empty -> head + dry-run", step_migrate),
    Step(3, "backend-tests", "backend test suite", step_backend_tests, slow_backend_tests=True),
    Step(4, "frontend", "frontend build + unit tests", step_frontend, needs_frontend=True),
    Step(5, "e2e", "fake-provider end-to-end (real server)", step_e2e),
    Step(6, "lifecycle", "pause/resume/retry/cancel over HTTP", step_lifecycle),
    Step(7, "restart", "restart/resume mid-pipeline", step_restart),
    Step(8, "isolation", "two-runtime / session isolation", step_isolation),
    Step(9, "artifacts", "artifact path-safety (uniform 404)", step_artifacts),
    Step(10, "backup", "backup + restore smoke", step_backup),
    Step(11, "shutdown", "startup + graceful shutdown", step_shutdown),
    Step(12, "real", "real-provider smoke (opt-in)", step_real),
    Step(13, "batch", "channel batch: skip, window, review, final.wav, retry, sync", step_batch),
]


# ------------------------------------------------------------------------------ main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="StoryFlow release smoke (final regression matrix)")
    p.add_argument("--only", help="comma-separated step numbers or ids (see --list)")
    p.add_argument("--skip-frontend", action="store_true", help="skip npm steps; the server runs with --no-frontend")
    p.add_argument("--skip-backend-tests", action="store_true", help="skip the full backend pytest run (step 3)")
    p.add_argument("--json", action="store_true", help="print the results as JSON on stdout (progress goes to stderr)")
    p.add_argument("--list", action="store_true", help="list the steps and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        for s in STEPS:
            print(f"{s.number:>2}  {s.id:<14} {s.name}")
        return 0
    try:
        only = parse_only(args.only, STEPS)
    except ValueError as exc:
        print(f"release_smoke: {exc}", file=sys.stderr)
        return 2
    out = sys.stderr if args.json else sys.stdout
    if not VENV_PY.exists():
        print(f"WARNING: {VENV_PY} not found; using {sys.executable} (run scripts\\setup.bat)", file=out)
    ctx = Ctx(args)
    results: list[Result] = []
    try:
        for step, reason in select_steps(STEPS, only, args.skip_frontend, args.skip_backend_tests):
            t0 = time.time()
            if reason:
                r = Result(step.number, step.id, step.name, SKIP, 0.0, reason)
            else:
                print(f"... {step.number:>2}. {step.name}", file=out, flush=True)
                try:
                    detail = step.fn(ctx)
                    r = Result(step.number, step.id, step.name, PASS, time.time() - t0, detail or "")
                except StepSkipped as exc:
                    r = Result(step.number, step.id, step.name, SKIP, time.time() - t0, str(exc))
                except StepFailure as exc:
                    r = Result(step.number, step.id, step.name, FAIL, time.time() - t0, str(exc))
                except subprocess.TimeoutExpired as exc:
                    r = Result(step.number, step.id, step.name, FAIL, time.time() - t0, f"timeout: {exc.cmd[-1:]}")
                except Exception as exc:  # noqa: BLE001 - a step must never abort the matrix
                    r = Result(step.number, step.id, step.name, FAIL, time.time() - t0,
                               f"{type(exc).__name__}: {exc}")
            results.append(r)
            print(format_result(r), file=out, flush=True)
    finally:
        ctx.cleanup()
    print(format_summary(results), file=out)
    if args.json:
        print(json.dumps({"ok": not any(r.status == FAIL for r in results),
                          "results": [asdict(r) for r in results]}, indent=2))
    return 0 if not any(r.status == FAIL for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
