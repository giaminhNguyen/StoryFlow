"""Phase 9 logging: configure_logging contract, redaction, and the structured lifecycle log lines."""

import logging
import re

import pytest

from storyflow.api.app import create_app
from storyflow.logging_config import (
    LOG_FILENAME, MAX_MESSAGE_CHARS, RedactingFilter, configure_logging, redact, resolve_level,
)
from test_api import ABS_PATH, ScanClient, discover_and_assign, make_runtime, new_workflow

MARK = "_storyflow_handler"


def _ours():
    return [h for h in logging.getLogger().handlers if getattr(h, MARK, False)]


@pytest.fixture
def handle_factory():
    handles = []

    def make(**kw):
        h = configure_logging(**kw)
        handles.append(h)
        return h
    yield make
    for h in handles:
        h.close()


def _flush(handle):
    for h in handle.handlers:
        h.flush()


def _read(handle):
    _flush(handle)
    return handle.log_file.read_text(encoding="utf-8")


# ------------------------------------------------------------------ configure_logging


def test_configure_is_idempotent_and_close_removes_handlers(tmp_path, handle_factory):
    root = logging.getLogger()
    level_before = root.level
    first = handle_factory(log_dir=tmp_path, console=True)
    second = handle_factory(log_dir=tmp_path, console=True)
    assert len(_ours()) == 2  # console + file, not 4
    logging.getLogger("storyflow.test").info("hello once")
    assert _read(second).count("hello once") == 1
    first.close()  # stale handle: must not remove the live ones
    assert len(_ours()) == 2
    second.close()
    assert _ours() == [] and root.level == level_before
    second.close()  # idempotent


def test_no_file_unless_requested_and_default_dir_from_env(tmp_path, handle_factory):
    h = handle_factory(console=False)
    assert h.log_file is None and h.handlers == []
    h.close()
    h = handle_factory(console=False, file=True, environ={"STORYFLOW_LOG_DIR": str(tmp_path / "envlogs")})
    assert h.log_file == tmp_path / "envlogs" / LOG_FILENAME and h.log_file.parent.is_dir()


def test_level_from_env_and_validation(tmp_path, handle_factory):
    assert resolve_level(None, {"STORYFLOW_LOG_LEVEL": "debug"}) == logging.DEBUG
    assert resolve_level("warning", {"STORYFLOW_LOG_LEVEL": "debug"}) == logging.WARNING
    assert resolve_level(None, {}) == logging.INFO
    with pytest.raises(ValueError):
        resolve_level("loud", {})
    h = handle_factory(log_dir=tmp_path, console=False, environ={"STORYFLOW_LOG_LEVEL": "warning"})
    log = logging.getLogger("storyflow.lvl")
    log.info("info-hidden")
    log.warning("warn-shown")
    text = _read(h)
    assert "warn-shown" in text and "info-hidden" not in text
    assert re.search(r"^\S+ \S+ WARNING storyflow\.lvl warn-shown$", text, re.M)


def test_unwritable_log_dir_raises_oserror_and_leaves_nothing(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(OSError):
        configure_logging(log_dir=blocker / "sub", console=True)
    assert _ours() == []


def test_rotation_is_bounded(tmp_path, handle_factory):
    h = handle_factory(log_dir=tmp_path, console=False, max_bytes=400, backups=2)
    log = logging.getLogger("storyflow.rot")
    for i in range(80):
        log.info("line %03d %s", i, "x" * 40)
    _flush(h)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [LOG_FILENAME, LOG_FILENAME + ".1", LOG_FILENAME + ".2"]
    assert all(p.stat().st_size < 400 + 200 for p in tmp_path.iterdir())


def test_uvicorn_asyncio_alembic_loggers_use_our_handlers(tmp_path, handle_factory):
    h = handle_factory(log_dir=tmp_path, console=False, level="debug")
    for name in ("uvicorn.error", "asyncio", "alembic.runtime.migration"):
        logging.getLogger(name).info("from %s", name)
    text = _read(h)
    assert "from uvicorn.error" in text and "from asyncio" in text and "from alembic.runtime.migration" in text


def test_uvicorn_access_drops_query_string(tmp_path, handle_factory):
    h = handle_factory(log_dir=tmp_path, console=False)
    access = logging.getLogger("uvicorn.access")
    access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:5000", "GET", "/api/workflows?id=abc123&key=hunter2", "1.1", 200)
    access.info('127.0.0.1:5000 - "POST /api/x?secret_id=zzz HTTP/1.1" 201')  # already-rendered variant
    text = _read(h)
    assert 'GET /api/workflows HTTP/1.1" 200' in text and 'POST /api/x HTTP/1.1" 201' in text
    for leaked in ("abc123", "hunter2", "secret_id", "zzz", "?"):
        assert leaked not in text


# ------------------------------------------------------------------ redaction


FAKE_SK = "sk-" + "A1b2C3d4E5f6G7h8I9j0"
FAKE_JWT = "Zm9vYmFyYmF6" * 6 + "Qw9"  # mixed-case base64-ish blob, 75 chars

REDACTION_CASES = [
    (f"key {FAKE_SK} used", ["A1b2C3d4E5"], "sk-***"),
    ("sk-ant-api03-abcdefghijklmnop failed", ["abcdefghijklmnop"], "sk-***"),
    ("header Authorization: Bearer abc.def.ghi123", ["abc.def"], "Authorization: ***"),
    ("Authorization=Basic dXNlcjpwYXNz", ["dXNlcjpwYXNz"], "Authorization=***"),
    ("got Bearer eyJhbGciOi.payload.sig ok", ["eyJhbGciOi"], "Bearer ***"),
    ("api_key=abcd1234 next", ["abcd1234"], "api_key=***"),
    ("password: 'p@ss w0rd' next", ["p@ss"], "password: ***"),
    ("token=xyz789&other=1", ["xyz789"], "token=***"),
    ("opened C:\\Users\\ming\\secret\\story.md now", ["ming", "story.md"], "<path>"),
    ("opened D:/data/projects/x.db now", ["projects"], "<path>"),
    ("opened /home/alice/work/x.txt now", ["alice"], "<path>"),
    ("opened /tmp/pytest-1/art now", ["pytest-1"], "<path>"),
    ("share \\\\server\\share\\file", ["server"], "<path>"),
    (f"blob {FAKE_JWT} end", [FAKE_JWT[:20]], "<blob>"),
]


@pytest.mark.parametrize("raw,gone,marker", REDACTION_CASES)
def test_redaction_table(raw, gone, marker):
    out = redact(raw)
    assert marker in out, out
    for g in gone:
        assert g not in out, out
    assert redact(out) == out  # idempotent


def test_redaction_keeps_ids_urls_and_short_tokens():
    keep = ("GET /api/workflows/3f2a9c1e0b7d4e5f8a6b1c2d3e4f5a6b/projects/abc HTTP/1.1 200",
            "workflow_command workflow=0123456789abcdef0123456789abcdef action=start changed=True status=active",
            "/assets/index-CzJ34hZd.js")
    for line in keep:
        assert redact(line) == line


def test_truncation_bounds_any_single_message():
    out = redact("story " * 5000)
    assert len(out) <= MAX_MESSAGE_CHARS + len("...[truncated]") and out.endswith("...[truncated]")


def test_filter_redacts_args_and_tracebacks(tmp_path, handle_factory):
    h = handle_factory(log_dir=tmp_path, console=False)
    log = logging.getLogger("storyflow.redact")
    log.info("using %s at %s", FAKE_SK, "C:\\Users\\ming\\x")
    try:
        raise RuntimeError("boom token=sekret123 at /home/bob/app/x.py")
    except RuntimeError:
        log.exception("failed")
    text = _read(h)
    for leaked in ("A1b2C3d4E5", "ming", "sekret123", "bob"):
        assert leaked not in text
    assert "Traceback" in text and "RuntimeError" in text
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "%s", ("secret path C:\\a\\b",), None)
    assert RedactingFilter().filter(rec) and "C:" not in rec.getMessage()


# ------------------------------------------------------------------ lifecycle log lines


def _lines(caplog, prefix):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(prefix)]


def _assert_clean(caplog, tmp_path):
    for r in caplog.records:
        if not r.name.startswith("storyflow"):
            continue  # httpx client chatter carries http:// URLs
        msg = r.getMessage()
        for banned in ("hero wakes", "rival waits", "darker turn", "narrator", "claim", str(tmp_path),
                       tmp_path.as_posix()):
            assert banned not in msg, msg
        assert not ABS_PATH.search(msg), msg


def test_command_transition_and_runner_lines(tmp_path, caplog):
    rt = make_runtime(tmp_path, sleep=lambda s: None)
    try:
        client = ScanClient(create_app(rt))
        with caplog.at_level(logging.INFO):
            wf = new_workflow(client)
            runner = discover_and_assign(rt, client, wf)
            client.post(f"/api/workflows/{wf}/start")
            client.post(f"/api/workflows/{wf}/start")  # idempotent no-op
            client.post(f"/api/workflows/{wf}/pause")
            client.post(f"/api/workflows/{wf}/resume")
            rt.test_router.story.emit_invalid = {"story"}
            for _ in range(60):
                rt.runtime.run_once()
                if _lines(caplog, "workflow_transition"):
                    break
            rt.test_router.story.emit_invalid = False
            client.post(f"/api/workflows/{wf}/retry")
            for _ in range(60):
                rt.runtime.run_once()
                if any("to=finished" in m for m in _lines(caplog, "workflow_transition")):
                    break
            client.post(f"/api/runners/{runner}/unassign")
            client.post(f"/api/workflows/{wf}/cancel")  # finished -> invalid_state: no line
    finally:
        rt.close()
    cmds = _lines(caplog, "workflow_command")
    assert f"workflow_command workflow={wf} action=create changed=True status=draft" in cmds
    assert f"workflow_command workflow={wf} action=start changed=True status=active" in cmds
    assert f"workflow_command workflow={wf} action=start changed=False status=active" in cmds
    assert f"workflow_command workflow={wf} action=pause changed=True status=paused" in cmds
    assert f"workflow_command workflow={wf} action=resume changed=True status=active" in cmds
    assert any("action=retry changed=True" in m for m in cmds)
    assert not any("action=cancel" in m for m in cmds)
    transitions = _lines(caplog, "workflow_transition")
    assert any(f"workflow={wf} from=active to=paused reason=step_failed" in m and "step=story" in m
               and "error_code=" in m and "project_id=" in m for m in transitions)
    assert any(f"workflow={wf} from=active to=finished" in m for m in transitions)
    assert any(m.startswith("step_failed project=") and "step=story" in m for m in caplog.messages)
    assert any(m.startswith("step_finalized project=") for m in caplog.messages)
    runner_lines = _lines(caplog, "runner_command")
    assert any(f"runner={runner} action=assign changed=True" in m for m in runner_lines)
    assert any(f"runner={runner} action=unassign changed=True" in m for m in runner_lines)
    assert any(m.startswith("runner_discovered runner=") for m in caplog.messages)
    assert all(r.levelno == logging.INFO for r in caplog.records
               if r.getMessage().split(" ")[0] in ("workflow_command", "workflow_transition", "runner_command"))
    _assert_clean(caplog, tmp_path)


def test_runner_health_flips_are_logged(tmp_path, caplog):
    rt = make_runtime(tmp_path, sleep=lambda s: None)
    try:
        provider = rt.providers[0]
        with caplog.at_level(logging.INFO):
            rt.supervisor.refresh()
            provider.set_health("r1", False)
            rt.supervisor.refresh()
            provider.set_health("r1", True)
            rt.supervisor.refresh()
    finally:
        rt.close()
    assert any(m.startswith("runner_health") and "change=offline error_code=unhealthy" in m for m in caplog.messages)
    assert any(m.startswith("runner_health") and "change=restored state=ready" in m for m in caplog.messages)
    _assert_clean(caplog, tmp_path)


def test_provider_error_is_logged_bounded(tmp_path, caplog):
    rt = make_runtime(tmp_path, sleep=lambda s: None)
    try:
        rt.providers[0].fail_detect = TimeoutError("detect " + "z" * 5000)
        with caplog.at_level(logging.WARNING):
            rt.supervisor.refresh()
    finally:
        rt.close()
    (msg,) = [m for m in caplog.messages if m.startswith("runner provider")]
    assert "TimeoutError" in msg and len(msg) < 1500


def test_loop_iteration_summary_is_debug_only(tmp_path, caplog):
    rt = make_runtime(tmp_path, sleep=lambda s: None)
    try:
        with caplog.at_level(logging.INFO):
            rt.runtime.run_forever(max_iterations=1)
        assert not [m for m in caplog.messages if m.startswith("iteration n=")]
        with caplog.at_level(logging.DEBUG, logger="storyflow.runtime.loop"):
            rt.runtime.run_forever(max_iterations=1)
        assert [m for m in caplog.messages if m.startswith("iteration n=")]
    finally:
        rt.close()
