"""Operations toolkit: doctor (tmp paths only; no network; providers driven by an explicit env dict)."""

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import storyflow.ops.doctor as doctor_mod
from storyflow.__main__ import main
from storyflow.ops import migrate_database, run_doctor
from test_ops_support import upgrade_to

BACKEND = Path(__file__).resolve().parent.parent
NO_PROVIDERS = {"STORYFLOW_SUBTITLE_PROVIDER": "none"}


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def workspace(tmp_path):
    db = tmp_path / "rt" / "storyflow.db"
    assert migrate_database(db, tmp_path / "b").ok
    (tmp_path / "fe" / "dist").mkdir(parents=True)
    (tmp_path / "fe" / "dist" / "index.html").write_text("<html></html>")
    ok_script = tmp_path / "bootstrap_ok.py"
    ok_script.write_text("import sys; sys.exit(0)")
    return {"db_path": db, "artifact_root": tmp_path / "rt" / "artifacts", "frontend_dir": tmp_path / "fe",
            "port": free_port(), "log_dir": tmp_path / "rt" / "logs", "provider_env": dict(NO_PROVIDERS),
            "bootstrap_script": ok_script}


def status(report, name):
    return next(c for c in report.checks if c.name == name)


def test_healthy_workspace_passes(workspace):
    rep = run_doctor(**workspace)
    assert rep.ok and rep.exit_code == 0
    assert not [c for c in rep.checks if c.status == "FAIL"]
    for name in ("packages", "bootstrap", "database", "database_integrity", "artifact_dir", "frontend", "port", "log_dir"):
        assert status(rep, name).status == "PASS", status(rep, name)
    assert status(rep, "python").status in ("PASS", "WARN")
    assert status(rep, "provider:subtitle").status == "INFO"  # nothing configured
    # read-only: it created nothing
    assert not workspace["artifact_root"].exists() and not workspace["log_dir"].exists()


def test_missing_db_is_ok_and_older_db_warns(workspace, tmp_path, monkeypatch):
    workspace["db_path"] = tmp_path / "none.db"
    rep = run_doctor(**workspace)
    assert rep.ok and "will be created" in status(rep, "database").message and not workspace["db_path"].exists()
    old = tmp_path / "old.db"
    upgrade_to(monkeypatch, old, "0003_story_domain")
    rep = run_doctor(**{**workspace, "db_path": old})
    st = status(rep, "database")
    assert st.status == "WARN" and "migrate" in st.fix and rep.ok


def test_newer_and_foreign_db_fail(workspace, tmp_path):
    import sqlite3
    con = sqlite3.connect(str(workspace["db_path"]))
    con.execute("UPDATE alembic_version SET version_num='0099_future'")
    con.commit()
    con.close()
    rep = run_doctor(**workspace)
    assert status(rep, "database").status == "FAIL" and not rep.ok and rep.exit_code == 1
    foreign = tmp_path / "foreign.db"
    con = sqlite3.connect(str(foreign))
    con.execute("CREATE TABLE t (x)")
    con.commit()
    con.close()
    rep = run_doctor(**{**workspace, "db_path": foreign})
    assert status(rep, "database").status == "FAIL"
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"junk" * 500)
    assert status(run_doctor(**{**workspace, "db_path": junk}), "database").status == "FAIL"


def test_unwritable_artifact_dir_fails(workspace, monkeypatch):
    def deny(directory):
        raise PermissionError("read-only file system")

    monkeypatch.setattr(doctor_mod, "_probe_writable", deny)
    rep = run_doctor(**workspace)
    assert status(rep, "artifact_dir").status == "FAIL" and not rep.ok
    assert status(rep, "log_dir").status == "FAIL" and status(rep, "artifact_dir").fix


def test_port_busy_missing_dist_and_bad_provider_warn(workspace, tmp_path):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    try:
        rep = run_doctor(**{**workspace, "port": blocker.getsockname()[1], "frontend_dir": tmp_path / "nofe",
                            "provider_env": {"STORYFLOW_STORY_RUNNER": "bogus"}})
    finally:
        blocker.close()
    assert status(rep, "port").status == "WARN" and "--port" in status(rep, "port").fix
    fe = status(rep, "frontend")
    assert fe.status == "WARN" and "npm run build" in fe.fix
    assert status(rep, "provider:story").status == "WARN" and "misconfigured" in status(rep, "provider:story").message
    assert rep.ok  # warnings never fail


def test_bootstrap_out_of_sync_warns(workspace, tmp_path):
    bad = tmp_path / "bootstrap_bad.py"
    bad.write_text("import sys; sys.exit(1)")
    rep = run_doctor(**{**workspace, "bootstrap_script": bad})
    assert status(rep, "bootstrap").status == "WARN" and "bootstrap.py" in status(rep, "bootstrap").fix and rep.ok


def test_quick_skips_heavy_checks(workspace):
    rep = run_doctor(**workspace, quick=True)
    assert status(rep, "database_integrity").message.startswith("skipped")
    assert status(rep, "bootstrap").message.startswith("skipped")


def test_corrupt_page_fails_integrity(workspace):
    p = workspace["db_path"]
    data = bytearray(p.read_bytes())
    for i in range(len(data) // 2, len(data) // 2 + 4096):  # scribble over the middle of the file
        data[i] = 0xAB
    p.write_bytes(bytes(data))
    rep = run_doctor(**workspace)
    assert not rep.ok and any(c.status == "FAIL" for c in rep.checks)


def _cli_args(w):
    return ["doctor", "--db", str(w["db_path"]), "--artifact-root", str(w["artifact_root"]), "--frontend-dir",
            str(w["frontend_dir"]), "--port", str(w["port"]), "--log-dir", str(w["log_dir"]), "--quick"]


def test_cli_json_shape_and_exit_codes(workspace, capsys, monkeypatch):
    for k, v in NO_PROVIDERS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr("storyflow.providers.default_env_files", lambda: [])
    code = main([*_cli_args(workspace), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0 and payload["ok"] is True
    assert all(set(c) == {"name", "status", "message", "fix"} for c in payload["checks"])
    assert {c["status"] for c in payload["checks"]} <= {"PASS", "INFO", "WARN", "FAIL"}
    assert main(_cli_args(workspace)) == 0
    assert "doctor: OK" in capsys.readouterr().out
    import sqlite3
    con = sqlite3.connect(str(workspace["db_path"]))
    con.execute("UPDATE alembic_version SET version_num='0099_future'")
    con.commit()
    con.close()
    assert main([*_cli_args(workspace), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
    assert main(["doctor", "--port", "not-a-number"]) == 2
    assert main([]) == 2


def test_real_subprocess_smoke(tmp_path):
    w = {"db_path": tmp_path / "s.db", "artifact_root": tmp_path / "art", "frontend_dir": tmp_path / "fe",
         "port": free_port(), "log_dir": tmp_path / "logs"}
    env = {**__import__("os").environ, "STORYFLOW_SUBTITLE_PROVIDER": "none"}
    proc = subprocess.run([sys.executable, "-m", "storyflow", *_cli_args(w), "--json"], cwd=str(BACKEND),
                          capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] and {c["name"] for c in payload["checks"]} >= {"python", "database", "port", "frontend"}
    assert not w["db_path"].exists() and not w["artifact_root"].exists()
