"""ClaudeCliRunner / ClaudeCliProvider over a FAKE claude CLI (tests/fake_claude_cli.py).

The real ``claude`` binary and the network are never touched: every subprocess is
``python fake_claude_cli.py <argv the runner built>`` through the injectable ``popen``.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from storyflow.agents import RunnerRegistry
from storyflow.artifacts import ArtifactStore
from storyflow.dispatcher import Dispatcher
from storyflow.integrations import claude_cli as cc
from storyflow.integrations.claude_cli import (
    ClaudeCliProvider, ClaudeCliRunner, build_canon_prompt, build_story_prompt, classify_failure,
    scrub_message,
)
from storyflow.models import RunnerAttempt, RunnerInstance, StoryVersion
from storyflow.pipeline import OutputValidatingRunner, StepStatus
from storyflow.protocol import ResultCode, TaskPacket
from storyflow.providers import READY, UNAVAILABLE, ProviderConfig
from storyflow.story_steps import STORY_VALIDATORS, CanonStep, StoryStep, validate_canon

from test_story_steps import (  # noqa: F401  (fixtures + helpers of the Phase 4 dispatcher-path tests)
    ctx, fresh, make_ctx, make_snapshot, project, reg_for, reload_job, run_job, session, store,
)

FAKE = Path(__file__).with_name("fake_claude_cli.py")
PID = "P1"


def fake_popen(argv, **kw):
    return subprocess.Popen([sys.executable, str(FAKE)] + list(argv[1:]), **kw)


def make_runner(store, tmp_path, monkeypatch, mode="success", *, timeout=30.0, model=None, which=None):

    def popen(argv, **kw):  # per-runner mode (env is otherwise process-global)
        kw["env"] = {**kw["env"], "FAKE_CLAUDE_MODE": mode}
        return fake_popen(argv, **kw)

    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(tmp_path / "record.json"))
    monkeypatch.setenv("FAKE_CLAUDE_PIDFILE", str(tmp_path / "pids.txt"))
    cfg = ProviderConfig(story_runner="claude-cli", claude_cli="claude", claude_model=model, story_timeout=timeout)
    return ClaudeCliRunner(cfg, store, popen=popen, which=which or (lambda name: "claude"))


def canon_packet(store, project_id=PID, analysis="A1", source="Once upon a time a rivalry began."):
    src = f"projects/{project_id}/source/0001/source.txt"
    store.write(src, source.encode())
    return TaskPacket(task_id="t-canon", role="story_writer",
                      inputs={"source_artifact": src, "snapshot_id": "s", "project_id": project_id},
                      outputs=[f"projects/{project_id}/canon/{analysis}/canon.json"],
                      task_config={"step": "canon"})


def story_packet(store, project_id=PID, gen="G1", **cfg):
    canon = store.write(f"projects/{project_id}/canon/A1/canon.json", json.dumps(_canon()).encode())
    src = f"projects/{project_id}/source/0001/source.txt"
    store.write(src, b"Once upon a time a rivalry began.")
    return TaskPacket(task_id="t-story", role="story_writer",
                      inputs={"source_artifact": src, "canon_artifact": canon, "project_id": project_id, **cfg},
                      outputs=[f"projects/{project_id}/story/{gen}/story.md"],
                      task_config={"step": "story", **cfg})


def _canon():
    sys.path.insert(0, str(FAKE.parent))
    import fake_claude_cli
    return fake_claude_cli.CANON


def record(tmp_path):
    return json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))


# --- success + invocation contract -----------------------------------------------------------------


def test_canon_success_writes_exact_path_and_metrics(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    packet = canon_packet(store)
    res = runner.execute(packet)
    assert res.code is ResultCode.SUCCESS and res.artifacts == {"outputs": [packet.outputs[0]]}
    assert validate_canon(json.loads(store.read(packet.outputs[0]))) is None
    assert res.metrics["duration_ms"] >= 0 and res.metrics["total_cost_usd"] == 0.0123
    assert res.metrics["input_tokens"] == 10 and res.metrics["output_tokens"] == 20
    assert not any(isinstance(v, str) for v in res.metrics.values())


def test_story_success_and_prompt_content(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    packet = story_packet(store, branch="the door stays shut", direction="dark", target_length=120)
    res = runner.execute(packet)
    assert res.code is ResultCode.SUCCESS
    text = store.read(packet.outputs[0]).decode()
    assert text.startswith("# The Other Door") and text.endswith("\n")
    stdin = record(tmp_path)["stdin"]
    assert "the door stays shut" in stdin and "about 120 words" in stdin and "Direction: dark" in stdin
    assert "story-branch-writer" in stdin and "central_conflict" in stdin


def test_exact_argv_stdin_cwd_env(store, tmp_path, monkeypatch):
    monkeypatch.setenv("STORYFLOW_SECRET_THING", "leak")
    monkeypatch.setenv("storyflow_lower", "leak")
    runner = make_runner(store, tmp_path, monkeypatch)
    packet = canon_packet(store, source="UNIQUE-SOURCE-MARKER")
    assert runner.execute(packet).code is ResultCode.SUCCESS
    rec = record(tmp_path)
    assert rec["argv"] == ["-p", "--output-format", "json", "--no-session-persistence", "--tools", "",
                           "--append-system-prompt", cc.SYSTEM_PROMPT]
    assert "--dangerously-skip-permissions" not in rec["argv"] and "--model" not in rec["argv"]
    assert "\n" not in cc.SYSTEM_PROMPT
    assert "UNIQUE-SOURCE-MARKER" in rec["stdin"]
    assert not any("UNIQUE-SOURCE-MARKER" in a for a in rec["argv"])
    cwd = Path(rec["cwd"])
    assert rec["cwd_entries"] == [] and not cwd.exists()  # empty temp dir, removed afterwards
    assert Path(rec["cwd"]).resolve() != Path.cwd().resolve()
    backend = Path(__file__).resolve().parent.parent
    assert backend not in cwd.resolve().parents and cwd.resolve() != backend
    assert not [k for k in rec["env_keys"] if k.upper().startswith("STORYFLOW_")]
    assert "PATH" in {k.upper() for k in rec["env_keys"]}


def test_model_flag_only_when_configured(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, model="sonnet")
    assert runner.execute(canon_packet(store)).code is ResultCode.SUCCESS
    argv = record(tmp_path)["argv"]
    assert argv[-2:] == ["--model", "sonnet"]


def test_fenced_outputs_are_unwrapped(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, "fenced")
    cp = canon_packet(store)
    assert runner.execute(cp).code is ResultCode.SUCCESS
    assert validate_canon(json.loads(store.read(cp.outputs[0]))) is None
    sp = story_packet(store)
    assert runner.execute(sp).code is ResultCode.SUCCESS
    assert "```" not in store.read(sp.outputs[0]).decode()


def test_unsupported_step_and_bad_paths(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    res = runner.execute(TaskPacket(task_config={"step": "tts"}))
    assert (res.code, res.error_code) == (ResultCode.TASK_FAILED, "unsupported_step")
    good = canon_packet(store)
    for bad_out in ("../evil/canon.json", "/abs/canon.json", "C:\\x\\canon.json",
                    f"projects/other/canon/A/canon.json", f"projects/{PID}/canon/A/other.json"):
        packet = TaskPacket(task_id="x", inputs=good.inputs, outputs=[bad_out], task_config={"step": "canon"})
        r = runner.execute(packet)
        assert (r.code, r.error_code) == (ResultCode.TASK_FAILED, "bad_path"), bad_out
    for bad_in in ("../secret.txt", "C:\\Windows\\win.ini", "projects/other/source/0001/source.txt", "a/b"):
        packet = TaskPacket(task_id="x", inputs={**good.inputs, "source_artifact": bad_in},
                            outputs=good.outputs, task_config={"step": "canon"})
        assert runner.execute(packet).error_code == "bad_path", bad_in
    assert not (tmp_path / "record.json").exists()  # CLI never started
    missing = TaskPacket(task_id="x", inputs={**good.inputs, "source_artifact": f"projects/{PID}/source/9/source.txt"},
                         outputs=good.outputs, task_config={"step": "canon"})
    assert runner.execute(missing).error_code == "missing_input"


# --- failures ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode,code,error_code", [
    ("rate_limit", ResultCode.RATE_LIMITED, "rate_limited"),
    ("overloaded_stderr", ResultCode.RATE_LIMITED, "rate_limited"),
    ("quota", ResultCode.QUOTA_EXHAUSTED, "quota_exhausted"),
    ("quota_credit", ResultCode.QUOTA_EXHAUSTED, "quota_exhausted"),
    ("auth", ResultCode.AUTH_ERROR, "auth_error"),
    ("auth_stderr", ResultCode.AUTH_ERROR, "auth_error"),
    ("crash", ResultCode.TRANSIENT_FAILURE, "cli_failed"),
    ("invalid_json", ResultCode.INVALID_OUTPUT, "invalid_envelope"),
    ("empty", ResultCode.INVALID_OUTPUT, "empty_result"),
    ("non_utf8", ResultCode.INVALID_OUTPUT, "bad_encoding"),
    ("giant", ResultCode.INVALID_OUTPUT, "output_too_large"),
    ("canon_no_json", ResultCode.INVALID_OUTPUT, "no_json"),
])
def test_classification_table(store, tmp_path, monkeypatch, mode, code, error_code):
    runner = make_runner(store, tmp_path, monkeypatch, mode)
    packet = canon_packet(store)
    res = runner.execute(packet)
    assert (res.code, res.error_code) == (code, error_code)
    assert not store.exists(packet.outputs[0])
    assert res.error_message and len(res.error_message) <= 300
    assert res.artifacts is None


def test_rate_limit_and_quota_hints(store, tmp_path, monkeypatch):
    res = make_runner(store, tmp_path, monkeypatch, "rate_limit").execute(canon_packet(store))
    assert res.retry_after == 30.0
    res = make_runner(store, tmp_path, monkeypatch, "quota").execute(canon_packet(store))
    assert res.quota_reset_at is not None and res.quota_reset_at.year == 2030
    res = make_runner(store, tmp_path, monkeypatch, "quota_credit").execute(canon_packet(store))
    assert res.quota_reset_at is None
    res = make_runner(store, tmp_path, monkeypatch, "overloaded_stderr").execute(canon_packet(store))
    assert res.retry_after is None


def test_classify_failure_text_table():
    assert classify_failure("boom") is None
    assert classify_failure("You have hit the usage limit").code is ResultCode.RATE_LIMITED
    assert classify_failure("5-hour limit reached, resets at 2030-01-01T10:00:00Z").quota_reset_at.year == 2030
    assert classify_failure("Not logged in").code is ResultCode.AUTH_ERROR
    assert classify_failure("authentication_error").code is ResultCode.AUTH_ERROR
    assert classify_failure("HTTP 429 Too Many Requests, retry-after: 12").retry_after == 12.0
    assert classify_failure("try again in 2 minutes: rate limit").retry_after == 120.0


def test_error_messages_scrubbed_and_bounded(store, tmp_path, monkeypatch):
    res = make_runner(store, tmp_path, monkeypatch, "crash").execute(canon_packet(store))
    assert "secret" not in res.error_message and "sk-" not in res.error_message
    dirty = "at C:\\Users\\bob\\x.py and /home/bob/y.py sk-abcdefghijk Bearer abc.def " + "A1b2C3d4" * 10
    clean = scrub_message(dirty + " z" * 400)
    assert len(clean) <= 300
    for leak in ("C:\\", "bob", "sk-abc", "abc.def", "A1b2C3d4A1b2"):
        assert leak not in clean


def test_cli_not_found(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, which=lambda name: None)
    res = runner.execute(canon_packet(store))
    assert (res.code, res.error_code) == (ResultCode.RUNNER_CRASHED, "cli_not_found")
    assert runner.health().ok is False

    def raising_popen(argv, **kw):
        raise FileNotFoundError("nope")

    runner._popen = raising_popen
    runner._which = lambda name: "claude"
    assert runner.execute(canon_packet(store)).error_code == "cli_not_found"


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_dead(pids, deadline=10.0):
    end = time.monotonic() + deadline
    while time.monotonic() < end and any(alive(p) for p in pids):
        time.sleep(0.1)
    return not any(alive(p) for p in pids)


def read_pids(tmp_path):
    end = time.monotonic() + 15
    while time.monotonic() < end:
        path = tmp_path / "pids.txt"
        if path.exists() and len(path.read_text().split()) == 2:
            return [int(x) for x in path.read_text().split()]
        time.sleep(0.05)
    raise AssertionError("fake CLI never wrote its pid file")


def test_timeout_kills_process_tree(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, "slow", timeout=3.0)
    started = time.monotonic()
    res = runner.execute(canon_packet(store))
    assert (res.code, res.error_code) == (ResultCode.TIMEOUT, "timeout")
    assert time.monotonic() - started < 30
    assert wait_dead(read_pids(tmp_path)), "orphaned CLI process survived the timeout"
    assert runner._procs == {}


def test_cancel_kills_running_process(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch, "slow", timeout=60.0)
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault("r", runner.execute(canon_packet(store))))
    thread.start()
    pids = read_pids(tmp_path)
    assert runner.cancel("t-canon") is True
    thread.join(30)
    assert not thread.is_alive() and box["r"].code is ResultCode.CANCELLED
    assert wait_dead(pids)
    assert runner.cancel("t-canon") is False and runner.cancel("unknown") is False


def test_classify_error(store, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    assert runner.classify_error(subprocess.TimeoutExpired("x", 1)) is ResultCode.TIMEOUT
    assert runner.classify_error(TimeoutError()) is ResultCode.TIMEOUT
    assert runner.classify_error(RuntimeError()) is ResultCode.RUNNER_CRASHED


def test_no_prompt_or_output_text_in_logs_or_results(store, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    for mode in ("success", "crash", "rate_limit", "invalid_json", "auth"):
        runner = make_runner(store, tmp_path, monkeypatch, mode)
        res = runner.execute(canon_packet(store, source="SECRET-SOURCE-TEXT-XYZ"))
        blob = repr(res)
        assert "SECRET-SOURCE-TEXT-XYZ" not in blob and "rivalry over a secret" not in blob
    res = make_runner(store, tmp_path, monkeypatch).execute(story_packet(store))
    assert "Other Door" not in repr(res)
    assert "SECRET-SOURCE-TEXT-XYZ" not in caplog.text and "Other Door" not in caplog.text


# --- prompt builders ---------------------------------------------------------------------------------


def test_prompt_builders_pure_and_deterministic():
    a = build_canon_prompt("SRC")
    assert a == build_canon_prompt("SRC") and "SRC" in a
    for key in ("central_conflict", "characters", "relationships", "events", "leverage_points", "ONLY one JSON"):
        assert key in a
    s = build_story_prompt('{"k": 1}', None, None, None)
    assert s == build_story_prompt('{"k": 1}', None, None, None)
    assert "3-5" in s and "ONLY the finished story" in s and '{"k": 1}' in s


# --- provider --------------------------------------------------------------------------------------


class Ran:
    def __init__(self, returncode=0, stdout="2.1.281 (Claude Code)\n", exc=None):
        self.calls, self.returncode, self.stdout, self.exc = [], returncode, stdout, exc

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, "")


def test_provider_detect_status_build_and_cache(store):
    ran, clock = Ran(), [0.0]
    provider = ClaudeCliProvider(ProviderConfig(story_runner="claude-cli"), store, which=lambda n: "C:\\bin\\claude.exe",
                                 run=ran, monotonic=lambda: clock[0])
    assert (provider.name, provider.roles, provider.max_concurrency) == ("claude-cli", ["story_writer"], 1)
    det = provider.detect()
    assert len(det) == 1 and det[0].runner_id == "claude-cli-1" and det[0].health.ok
    status = provider.status()
    assert (status.name, status.kind, status.state) == ("claude-cli", "story", READY)
    assert status.details == {"version": "2.1.281 (Claude Code)"} and "claude.exe" not in repr(status)
    argv, kw = ran.calls[0]
    assert argv[1:] == ["--version"] and kw["timeout"] == 10.0
    assert not [k for k in kw["env"] if k.upper().startswith("STORYFLOW_")]
    provider.detect()
    assert len(ran.calls) == 1  # cached
    clock[0] = 61.0
    provider.detect()
    assert len(ran.calls) == 2
    assert isinstance(provider.build("claude-cli-1"), ClaudeCliRunner)
    assert provider.build("claude-cli-1").runner_type == "claude-cli"


@pytest.mark.parametrize("kwargs", [
    {"which": lambda n: None},
    {"which": lambda n: "claude", "run": Ran(returncode=1)},
    {"which": lambda n: "claude", "run": Ran(exc=subprocess.TimeoutExpired("claude", 10))},
    {"which": lambda n: "claude", "run": Ran(exc=OSError("x"))},
])
def test_provider_unavailable(store, kwargs):
    provider = ClaudeCliProvider(ProviderConfig(story_runner="claude-cli"), store, **kwargs)
    assert provider.detect()[0].health.ok is False
    status = provider.status()
    assert status.state == UNAVAILABLE and status.message == "claude CLI not found on PATH; set STORYFLOW_CLAUDE_CLI"
    assert status.details == {}


# --- full pipeline through the real Dispatcher -----------------------------------------------------


def wrapped(runner, store):
    return OutputValidatingRunner(runner, store, STORY_VALIDATORS)


def run_canon(db, ctx, project, store, session, runner):
    make_snapshot(ctx, project)
    step = CanonStep()
    aid, spec = step.begin(db, ctx, project)
    job, outcome = run_job(db, session, wrapped(runner, store), spec)
    step.finalize(db, ctx, aid, job)
    return aid, job


def test_full_pipeline_canon_and_story_produce_story_version(db, ctx, project, store, session, tmp_path, monkeypatch):
    runner = make_runner(store, tmp_path, monkeypatch)
    aid, cjob = run_canon(db, ctx, project, store, session, runner)
    assert reload_job(db, cjob.id).status == "succeeded" or reload_job(db, cjob.id).status == "completed"
    assert CanonStep().status(db, ctx, project).status is StepStatus.COMPLETED
    step = StoryStep()
    gid, spec = step.begin(db, ctx, project)
    job, _ = run_job(db, session, wrapped(runner, store), spec)
    step.finalize(db, ctx, gid, job)
    version = db.scalars(select(StoryVersion)).one()
    assert version.story_generation_id == gid and "Other Door" in version.content
    assert version.content_path == f"projects/{project.id}/story/{gid}/story.md"
    assert step.status(db, ctx, project).status is StepStatus.COMPLETED


def test_invalid_story_output_is_business_failure(db, ctx, project, store, session, tmp_path, monkeypatch):
    good = make_runner(store, tmp_path, monkeypatch)
    run_canon(db, ctx, project, store, session, good)
    bad = make_runner(store, tmp_path, monkeypatch, "bad_story")
    step = StoryStep()
    gid, spec = step.begin(db, ctx, project)
    job, _ = run_job(db, session, wrapped(bad, store), spec)
    j = reload_job(db, job.id)
    assert j.attempts == 1 and j.infrastructure_failures == 0 and j.status == "queued"
    attempt = db.scalars(select(RunnerAttempt).where(RunnerAttempt.pipeline_job_id == job.id)).one()
    assert attempt.result_type == "invalid_output"
    assert db.scalars(select(StoryVersion)).all() == []


def test_quota_failover_to_second_runner(db, ctx, project, store, session, tmp_path, monkeypatch):
    good = make_runner(store, tmp_path, monkeypatch)
    run_canon(db, ctx, project, store, session, good)
    quota = make_runner(store, tmp_path, monkeypatch, "quota")
    second = RunnerInstance(workflow_session_id=session.id, runner_type="claude-cli", max_concurrency=2,
                            supported_roles=["story_writer"])
    db.add(second)
    db.commit()
    first = db.scalars(select(RunnerInstance).where(RunnerInstance.id != second.id)).first()
    reg = RunnerRegistry()
    # the dispatcher prefers the least recently used instance: `second` (never used) goes first
    reg.register(second.id, wrapped(quota, store))
    reg.register(first.id, wrapped(make_runner(store, tmp_path, monkeypatch), store))
    step = StoryStep()
    gid, spec = step.begin(db, ctx, project)
    from storyflow import queue
    job = queue.enqueue_job(db, kind=spec.kind, payload=spec.payload, dedupe_key=spec.dedupe_key,
                            priority=spec.priority, role=spec.role, session_id=session.id)
    dispatcher = Dispatcher(reg)
    for _ in range(4):
        dispatcher.run_round(db, session)
        if db.scalars(select(StoryVersion)).all() or store.exists(spec.payload["outputs"][0]):
            break
    step.finalize(db, ctx, gid, reload_job(db, job.id))
    assert fresh(db, RunnerInstance, second.id).state == "quota_exhausted"
    assert db.scalars(select(StoryVersion)).one().story_generation_id == gid
    types = [a.result_type for a in db.scalars(select(RunnerAttempt).where(RunnerAttempt.pipeline_job_id == job.id))]
    assert "quota_exhausted" in types and types[-1] == "success" and reload_job(db, job.id).attempts == 0
