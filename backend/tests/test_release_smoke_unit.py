"""Fast unit tests for scripts/release_smoke.py (never starts a server or runs a subprocess)."""

from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "release_smoke.py"
_spec = importlib.util.spec_from_file_location("release_smoke", _PATH)
rs = importlib.util.module_from_spec(_spec)
sys.modules["release_smoke"] = rs
_spec.loader.exec_module(rs)


def test_free_port_is_bindable_and_in_range():
    port = rs.free_port()
    assert 1024 <= port <= 65535
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))


def test_registry_is_complete_and_unique():
    numbers = [s.number for s in rs.STEPS]
    ids = [s.id for s in rs.STEPS]
    assert numbers == list(range(1, 13))
    assert len(set(ids)) == len(ids)
    assert all(callable(s.fn) for s in rs.STEPS)
    assert {s.id for s in rs.STEPS if s.needs_frontend} == {"frontend"}
    assert {s.id for s in rs.STEPS if s.slow_backend_tests} == {"backend-tests"}


def test_parse_only_accepts_numbers_and_ids():
    assert rs.parse_only("5, 6,shutdown", rs.STEPS) == ["e2e", "lifecycle", "shutdown"]
    assert rs.parse_only(None, rs.STEPS) == []
    with pytest.raises(ValueError):
        rs.parse_only("99", rs.STEPS)
    with pytest.raises(ValueError):
        rs.parse_only("nonsense", rs.STEPS)


def test_select_steps_flags():
    chosen = dict((s.id, r) for s, r in rs.select_steps(rs.STEPS, [], True, True))
    assert chosen["frontend"] == "--skip-frontend"
    assert chosen["backend-tests"] == "--skip-backend-tests"
    assert chosen["e2e"] is None
    only = rs.select_steps(rs.STEPS, ["migrate", "e2e"], False, False)
    assert [s.id for s, _ in only] == ["migrate", "e2e"]


def test_build_server_cmd_uses_frontend_or_no_frontend(tmp_path):
    cmd = rs.build_server_cmd("py", 1234, tmp_path / "a.db", tmp_path / "art", tmp_path / "logs", tmp_path / "dist")
    assert cmd[:4] == ["py", "-m", "storyflow.api", "--fake"]
    assert cmd[cmd.index("--port") + 1] == "1234"
    assert cmd[cmd.index("--database-url") + 1].startswith("sqlite:///")
    assert "\\" not in cmd[cmd.index("--database-url") + 1]
    assert cmd[cmd.index("--frontend-dir") + 1] == str(tmp_path / "dist")
    assert "--no-frontend" not in cmd
    bare = rs.build_server_cmd("py", 1, tmp_path / "a.db", tmp_path, tmp_path, None)
    assert "--no-frontend" in bare and "--frontend-dir" not in bare


def test_build_cli_cmd():
    assert rs.build_cli_cmd("py", "backup", "--json") == ["py", "-m", "storyflow", "backup", "--json"]


def test_result_and_summary_formatting():
    ok = rs.Result(1, "bootstrap", "bootstrap pins", rs.PASS, 1.234, "fine")
    bad = rs.Result(2, "migrate", "migration", rs.FAIL, 0.5, "boom")
    skipped = rs.Result(3, "backend-tests", "backend", rs.SKIP, 0.0, "--skip-backend-tests")
    assert rs.format_result(ok) == "[PASS]  1. bootstrap pins (1.2s) - fine"
    assert "[FAIL]" in rs.format_result(bad)
    text = rs.format_summary([ok, bad, skipped])
    assert "1 passed, 1 failed, 1 skipped" in text and "RELEASE SMOKE FAIL" in text
    assert "RELEASE SMOKE PASS" in rs.format_summary([ok, skipped])


def test_tail_and_artifact_url():
    assert rs.tail("a\n\nb\nc", 2) == "b | c"
    assert rs.artifact_url("projects/x y/a.wav") == "/api/artifacts/projects/x%20y/a.wav"


def test_clean_env_drops_storyflow_vars(monkeypatch):
    monkeypatch.setenv("STORYFLOW_STORY_RUNNER", "claude-cli")
    env = rs.clean_env({"X": "1"})
    assert not any(k.startswith("STORYFLOW_") for k in env)
    assert env["X"] == "1" and env["PYTHONPATH"].endswith("backend")
