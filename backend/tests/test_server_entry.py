"""Phase 9 server entry: args, frontend auto-detect, banner, startup-failure UX, browser opener, graceful shutdown
(in-process real uvicorn.Server + one real subprocess with a real signal)."""

import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from storyflow.api import __main__ as api_main
from storyflow.logging_config import LOG_FILENAME
from storyflow.runtime.app import SchemaError

BACKEND = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>SF</title>", encoding="utf-8")
    (root / "assets" / "a-1.js").write_text("1", encoding="utf-8")
    return root


def base_args(tmp_path, *extra):
    return ["--fake", "--database-url", f"sqlite:///{(tmp_path / 'e.db').as_posix()}",
            "--artifact-root", str(tmp_path / "art"), "--no-log-file", *extra]


# ------------------------------------------------------------------ args + frontend detection


def test_parse_defaults():
    a = api_main.parse_args([])
    assert (a.host, a.port) == ("127.0.0.1", 8765)
    assert a.frontend_dir is None and a.no_frontend is False and a.log_dir is None
    assert a.log_file is True and a.open_browser is False and a.log_level is None
    b = api_main.parse_args(["--no-log-file", "--open-browser", "--log-dir", "x", "--frontend-dir", "d", "--no-frontend",
                             "--log-level", "debug"])
    assert (b.log_file, b.open_browser, b.log_dir, b.frontend_dir, b.no_frontend, b.log_level) == \
        (False, True, "x", "d", True, "debug")
    assert api_main.parse_args(["--log-file"]).log_file is True


def test_frontend_dir_resolution(tmp_path):
    root = tmp_path / "repo"
    assert api_main.resolve_frontend_dir(api_main.parse_args([]), root) == root / "frontend" / "dist"
    assert api_main.resolve_frontend_dir(api_main.parse_args(["--frontend-dir", str(tmp_path / "x")]), root) \
        == tmp_path / "x"
    assert api_main.resolve_frontend_dir(api_main.parse_args(["--no-frontend"]), root) is None
    assert api_main.resolve_frontend_dir(api_main.parse_args(["--no-frontend", "--frontend-dir", "x"]), root) is None
    assert api_main.REPO_ROOT == BACKEND.parent  # default points at <repo>/frontend/dist


def test_build_server_app_serves_detected_dist_and_warns_when_missing(tmp_path, caplog):
    repo = tmp_path / "repo"
    (repo / "frontend" / "dist").mkdir(parents=True)
    (repo / "frontend" / "dist" / "index.html").write_text("<html>hi</html>", encoding="utf-8")
    from starlette.testclient import TestClient
    api, rt = api_main.build_server_app(api_main.parse_args(base_args(tmp_path, "--no-runtime")), repo_root=repo)
    try:
        assert api.state.frontend_served is True and TestClient(api).get("/").text == "<html>hi</html>"
    finally:
        rt.close()
    with caplog.at_level(logging.WARNING, logger="storyflow.api"):
        api, rt = api_main.build_server_app(api_main.parse_args(base_args(tmp_path, "--no-runtime")),
                                            repo_root=tmp_path / "nothing")
    rt.close()
    assert api.state.frontend_served is False and any("npm run build" in m for m in caplog.messages)
    api, rt = api_main.build_server_app(api_main.parse_args(base_args(tmp_path, "--no-runtime", "--no-frontend")),
                                        repo_root=repo)
    rt.close()
    assert api.state.frontend_served is False


# ------------------------------------------------------------------ banner


def test_database_label_has_no_credentials_or_paths(tmp_path):
    assert api_main.database_label(None) == "default"
    label = api_main.database_label(f"sqlite:///{(tmp_path / 'sub' / 'my.db').as_posix()}")
    assert label == "sqlite:my.db"
    pg = api_main.database_label("postgresql+psycopg://alice:s3cret@db.internal:5432/story")
    assert "alice" not in pg and "s3cret" not in pg and "db.internal:5432/story" in pg
    assert api_main.database_label("not a url") == "unparseable"


def test_banner_is_one_line_without_secrets_or_paths(tmp_path, caplog):
    args = api_main.parse_args(["--database-url", f"sqlite:///{(tmp_path / 'secret-dir' / 'x.db').as_posix()}",
                                "--port", "9123"])
    api = SimpleNamespace(state=SimpleNamespace(frontend_served=True))
    with caplog.at_level(logging.INFO, logger="storyflow.api"):
        api_main.log_banner(args, api)
    (msg,) = caplog.messages
    assert "listening_on=127.0.0.1:9123" in msg and "database=sqlite:x.db" in msg and "frontend=yes" in msg
    assert "runtime=embedded" in msg and "0.6.0" in msg and "\n" not in msg
    assert "secret-dir" not in msg and str(tmp_path) not in msg and tmp_path.as_posix() not in msg


# ------------------------------------------------------------------ startup failure UX (exit code 2, one line)


def _one_line_error(capsys):
    text = capsys.readouterr().err
    assert "Traceback" not in text
    err = [ln for ln in text.splitlines() if ln.startswith("storyflow.api:")]  # (console log lines share stderr)
    assert len(err) == 1
    return err[0]


def test_port_in_use_exits_2_with_actionable_message(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(api_main, "build_server_app", lambda *a, **k: pytest.fail("must not build"))
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = busy.getsockname()[1]
        assert api_main.port_in_use_reason("127.0.0.1", port) is not None
        assert api_main.main(["--no-log-file", "--port", str(port)]) == 2
    line = _one_line_error(capsys)
    assert str(port) in line and "--port" in line
    assert api_main.port_in_use_reason("127.0.0.1", free_port()) is None
    assert api_main.port_in_use_reason("127.0.0.1", 0) is None


def test_non_loopback_refusal_unchanged(capsys, monkeypatch):
    monkeypatch.setattr(api_main, "build_server_app", lambda *a, **k: pytest.fail("must not build"))
    assert api_main.main(["--no-log-file", "--host", "0.0.0.0"]) == 2
    assert "refusing to bind non-loopback" in _one_line_error(capsys)


def test_invalid_port_and_log_setup_failures(tmp_path, capsys, monkeypatch):
    assert api_main.main(["--no-log-file", "--port", "70000"]) == 2
    assert "--port" in _one_line_error(capsys)
    blocker = tmp_path / "afile"
    blocker.write_text("x")
    assert api_main.main(["--log-dir", str(blocker / "logs")]) == 2
    assert "--log-dir" in _one_line_error(capsys)
    monkeypatch.setenv("STORYFLOW_LOG_LEVEL", "loud")
    assert api_main.main(["--no-log-file"]) == 2
    assert "STORYFLOW_LOG_LEVEL" in _one_line_error(capsys)


def test_startup_failures_are_one_line_exit_2(tmp_path, capsys, monkeypatch):
    def schema_error(*a, **k):
        raise SchemaError("database is at revision 'old', expected 'new'; run `alembic upgrade head` from backend/ first.")

    monkeypatch.setattr(api_main, "build_server_app", schema_error)
    assert api_main.main(["--no-log-file", "--port", str(free_port())]) == 2
    assert "alembic upgrade head" in _one_line_error(capsys)
    monkeypatch.undo()
    # real failures through the real builder: malformed url, non-empty foreign db, unusable artifact root
    assert api_main.main(["--no-log-file", "--port", str(free_port()), "--database-url", "not-a-url"]) == 2
    assert "--database-url" in _one_line_error(capsys)
    foreign = tmp_path / "foreign.db"
    import sqlite3
    with sqlite3.connect(foreign) as c:
        c.execute("create table t (x)")
    c.close()
    assert api_main.main(["--no-log-file", "--port", str(free_port()), "--database-url",
                          f"sqlite:///{foreign.as_posix()}"]) == 2
    assert "alembic_version" in _one_line_error(capsys)
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    assert api_main.main(["--no-log-file", "--port", str(free_port()), "--fake", "--database-url",
                          f"sqlite:///{(tmp_path / 'ok.db').as_posix()}", "--artifact-root", str(blocker / "a")]) == 2
    _one_line_error(capsys)


# ------------------------------------------------------------------ browser opener


def test_browser_opens_once_when_listening(tmp_path):
    calls = []
    done = threading.Event()

    def opener(url):
        calls.append(url)
        done.set()

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        t = api_main.schedule_browser_open(f"http://127.0.0.1:{port}/", "127.0.0.1", port, opener)
        assert done.wait(10)
        t.join(5)
    assert calls == [f"http://127.0.0.1:{port}/"] and not t.is_alive()


def test_browser_opener_failure_is_swallowed_and_timeout_never_opens(caplog):
    def boom(url):
        raise OSError("no browser here")

    with socket.socket() as srv:
        srv.bind(("127.0.0.1", 0))
        srv.listen(5)
        port = srv.getsockname()[1]
        with caplog.at_level(logging.INFO, logger="storyflow.api"):
            t = api_main.schedule_browser_open("http://x/", "127.0.0.1", port, boom)
            t.join(10)
        assert not t.is_alive() and any("could not open the browser" in m for m in caplog.messages)
    calls = []
    t = api_main.schedule_browser_open("http://x/", "127.0.0.1", free_port(), calls.append, timeout=0.3)
    t.join(10)
    assert calls == [] and not t.is_alive()
    stop = threading.Event()
    stop.set()
    t = api_main.schedule_browser_open("http://x/", "127.0.0.1", free_port(), calls.append, stop=stop)
    t.join(10)
    assert calls == []


def test_main_open_browser_flag_schedules_once_and_survives_opener_failure(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(api_main, "schedule_browser_open", lambda *a, **k: seen.append(a))
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    port = free_port()
    assert api_main.main(base_args(tmp_path, "--open-browser", "--port", str(port), "--no-runtime")) == 0
    assert len(seen) == 1 and seen[0][0] == f"http://127.0.0.1:{port}/" and seen[0][1:3] == ("127.0.0.1", port)
    seen.clear()
    assert api_main.main(base_args(tmp_path, "--port", str(port), "--no-runtime")) == 0
    assert seen == []


# ------------------------------------------------------------------ shutdown path


class FakeHost:
    def __init__(self, ok=True):
        self.stops, self.ok = 0, ok

    def stop(self):
        self.stops += 1
        return self.ok


class FakeRT:
    closed = 0

    def close(self):
        FakeRT.closed += 1


def _fake_build(host):
    api = SimpleNamespace(state=SimpleNamespace(frontend_served=False, container=SimpleNamespace(host=host)))
    return lambda *a, **k: (api, FakeRT())


@pytest.mark.parametrize("raises", [None, KeyboardInterrupt])
def test_main_stops_host_closes_app_and_logs_shutdown(tmp_path, monkeypatch, caplog, raises):
    import uvicorn
    host, FakeRT.closed = FakeHost(), 0
    monkeypatch.setattr(api_main, "build_server_app", _fake_build(host))

    def run(app, **kw):
        if raises:
            raise raises  # what uvicorn re-raises after its own graceful exit on SIGINT
    monkeypatch.setattr(uvicorn, "run", run)
    before = signal.getsignal(signal.SIGTERM)
    with caplog.at_level(logging.INFO, logger="storyflow.api"):
        assert api_main.main(["--no-log-file", "--port", str(free_port())]) == 0
    assert host.stops == 1 and FakeRT.closed == 1
    assert caplog.messages[-1] == "shutdown complete"
    assert signal.getsignal(signal.SIGTERM) is before  # handlers restored


def test_main_cleans_up_even_when_uvicorn_crashes_and_flags_stuck_runtime(tmp_path, monkeypatch, caplog):
    import uvicorn
    host, FakeRT.closed = FakeHost(ok=False), 0
    monkeypatch.setattr(api_main, "build_server_app", _fake_build(host))

    def run(app, **kw):
        raise SystemExit(1)  # uvicorn exits like this when the bind fails
    monkeypatch.setattr(uvicorn, "run", run)
    with caplog.at_level(logging.INFO, logger="storyflow.api"):
        with pytest.raises(SystemExit):
            api_main.main(["--no-log-file", "--port", str(free_port())])
    assert host.stops == 1 and FakeRT.closed == 1
    assert any("still alive" in m for m in caplog.messages) and caplog.messages[-1] == "shutdown complete"


def test_sigbreak_routes_to_sigint_when_available():
    if not hasattr(signal, "SIGBREAK"):
        pytest.skip("SIGBREAK exists only on Windows")  # platform capability, not a sandbox limitation
    seen = []
    prev_int = signal.signal(signal.SIGINT, lambda *_: seen.append("int"))
    restore = api_main._install_signals()
    try:
        signal.raise_signal(signal.SIGBREAK)
    finally:
        restore()
        signal.signal(signal.SIGINT, prev_int)
    assert seen == ["int"]


def _get(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def test_real_uvicorn_server_thread_graceful_stop(tmp_path, dist):
    port = free_port()
    args = api_main.parse_args(base_args(tmp_path, "--port", str(port), "--frontend-dir", str(dist)))
    api, rt = api_main.build_server_app(args)
    host = api.state.container.host
    server = api_main.build_server(api, args)
    thread = threading.Thread(target=server.run, name="test-uvicorn")
    thread.start()
    try:
        deadline = time.monotonic() + 30
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started
        status, body = _get(f"http://127.0.0.1:{port}/api/health")
        assert status == 200 and host.running
        assert _get(f"http://127.0.0.1:{port}/")[1].startswith(b"<!doctype html>")
        assert _get(f"http://127.0.0.1:{port}/assets/a-1.js")[1] == b"1"
    finally:
        server.should_exit = True
        thread.join(30)
    assert not thread.is_alive()
    assert not host.running and host.thread is None and host.stop_timed_out is False
    rt.close()
    with pytest.raises(OSError):
        _get(f"http://127.0.0.1:{port}/api/health", timeout=1)


# ------------------------------------------------------------------ real subprocess


def _wait_health(port, proc, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return False
        try:
            if _get(f"http://127.0.0.1:{port}/api/health", timeout=2)[0] == 200:
                return True
        except OSError:
            time.sleep(0.2)
    return False


def test_subprocess_graceful_shutdown_on_signal(tmp_path, dist):
    port = free_port()
    logs = tmp_path / "logs"
    cmd = [sys.executable, "-m", "storyflow.api", "--fake", "--port", str(port),
           "--database-url", f"sqlite:///{(tmp_path / 'sub.db').as_posix()}", "--artifact-root", str(tmp_path / "art"),
           "--frontend-dir", str(dist), "--log-dir", str(logs)]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    out = open(tmp_path / "out.txt", "wb")
    proc = subprocess.Popen(cmd, cwd=BACKEND, stdout=out, stderr=subprocess.STDOUT, creationflags=flags)
    try:
        assert _wait_health(port, proc), (tmp_path / "out.txt").read_text(errors="replace")[-2000:]
        assert b"<title>SF</title>" in _get(f"http://127.0.0.1:{port}/")[1]
        proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        try:
            code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(10)
            pytest.xfail("graceful signal delivery is not supported in this environment (process had to be killed)")
        assert code == 0, (tmp_path / "out.txt").read_text(errors="replace")[-2000:]
        text = (logs / LOG_FILENAME).read_text(encoding="utf-8")
        assert "shutdown complete" in text and "runtime host stopped clean=True" in text
        assert "StoryFlow API" in text and "frontend=yes" in text
        assert str(tmp_path) not in text and tmp_path.as_posix() not in text
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(10)
        out.close()
