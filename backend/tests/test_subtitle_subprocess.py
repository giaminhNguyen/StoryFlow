"""Subtitle worker subprocess: real worker script + real subprocess over a FAKE upstream package.

No network: the fake upstream (app/services/subtitles.py under tmp_path) mimics the upstream public
API names/shapes. Only the deliberate hard-timeout test waits on a sleeping child.
"""

import json
import os
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

import pytest

from storyflow.artifacts import ArtifactStore
from storyflow.integrations import subtitle_subprocess as sp
from storyflow.integrations.subtitle_subprocess import SubprocessSubtitleClient
from storyflow.models import ChannelWorkflow, SourceSnapshot, StoryProject
from storyflow.pipeline import PipelineContext, StepStatus
from storyflow.providers import READY, UNAVAILABLE, ProviderConfig
from storyflow.story_steps import SourceStep
from storyflow.subtitles import (
    BlockedByProvider,
    FakeSubtitleClient,
    LanguageUnavailable,
    ProviderTimeout,
    ProviderUnavailable,
    SubtitlesUnavailable,
)

FAKE_UPSTREAM = textwrap.dedent('''
    import os, sys, time

    class SubtitleUnavailable(Exception): pass
    class LanguageUnavailable(Exception): pass
    class BlockedByYouTube(Exception): pass

    class _T:
        language = "Tiếng Việt"
        language_code = "vi"
        is_generated = False
        is_translatable = True

    def _behave(video_id):
        if video_id == "blocked": raise BlockedByYouTube("429 too many requests")
        if video_id == "nosub": raise SubtitleUnavailable("disabled")
        if video_id == "lang": raise LanguageUnavailable("no such language")
        if video_id == "boom": raise RuntimeError("cannot open C:\\\\secret\\\\dir\\\\file.txt and /home/u/x/y.db")
        if video_id == "net": raise ConnectionError("connection reset by peer")
        if video_id == "crash": os._exit(3)
        if video_id == "chatty": print("stray print on stdout")
        if video_id == "sleep":
            with open(os.environ["FAKE_PID_FILE"], "w") as fh:
                fh.write(str(os.getpid()))
            time.sleep(60)

    def available_transcripts(video_id):
        _behave(video_id)
        return [{"language": "Tiếng Việt", "language_code": "vi", "is_generated": False, "is_translatable": True},
                {"language": "English", "language_code": "en", "is_generated": True, "is_translatable": False}]

    def fetch_selected(video_id, languages, preference, allow_translation):
        _behave(video_id)
        return _T(), False, [{"text": "Xin chào thế giới", "start": 0.0, "duration": 1.5},
                             {"text": "Tạm biệt", "start": 2.0}]
''')

BROKEN_UPSTREAM = "import definitely_missing_module_xyz\n"


def _make_upstream(root: Path, body: str) -> Path:
    backend = root / "backend"
    (backend / "app" / "services").mkdir(parents=True)
    (backend / "app" / "__init__.py").write_text("")
    (backend / "app" / "services" / "__init__.py").write_text("")
    (backend / "app" / "services" / "subtitles.py").write_text(body, encoding="utf-8")
    return backend


@pytest.fixture(scope="module")
def upstream(tmp_path_factory):
    return _make_upstream(tmp_path_factory.mktemp("upstream"), FAKE_UPSTREAM)


@pytest.fixture(scope="module")
def broken_upstream(tmp_path_factory):
    return _make_upstream(tmp_path_factory.mktemp("broken"), BROKEN_UPSTREAM)


def cfg(backend, **kw):
    return ProviderConfig(subtitle_python=kw.pop("python", sys.executable),
                          subtitle_backend_dir=str(backend), subtitle_timeout=kw.pop("timeout", 30.0), **kw)


def assert_path_free(message: str):
    assert "\\" not in message and "/home" not in message and ":/" not in message
    assert str(Path.home()) not in message and "secret" not in message


# --- protocol over a real subprocess ---------------------------------------------------


def test_list_and_fetch_roundtrip_utf8(upstream):
    client = SubprocessSubtitleClient(cfg(upstream))
    tracks = client.list_tracks("ok")
    assert [(t.language_code, t.is_generated, t.is_translatable) for t in tracks] == [
        ("vi", False, True), ("en", True, False)]
    assert tracks[0].language == "Tiếng Việt"
    fetched = client.fetch("ok", ["vi"], "manual", False)
    assert fetched.language == "Tiếng Việt" and fetched.translated is False
    assert [(s.text, s.start, s.duration) for s in fetched.snippets] == [
        ("Xin chào thế giới", 0.0, 1.5), ("Tạm biệt", 2.0, 0.0)]


def test_stray_stdout_does_not_corrupt_protocol(upstream):
    assert SubprocessSubtitleClient(cfg(upstream)).list_tracks("chatty")


def run_worker(op, request, python=sys.executable):
    done = subprocess.run([python, str(sp.WORKER_PATH), op], input=json.dumps(request).encode(),
                          capture_output=True, timeout=60)
    return done.returncode, json.loads(done.stdout.decode("utf-8"))


@pytest.mark.parametrize("video_id,error", [
    ("blocked", "blocked"), ("nosub", "no_subtitle"), ("lang", "language_unavailable"), ("boom", "internal"),
    ("net", "network")])
def test_worker_error_encoding_exit_zero(upstream, video_id, error):
    code, out = run_worker("fetch", {"backend_dir": str(upstream), "video_id": video_id})
    assert code == 0 and out["ok"] is False and out["error"] == error
    assert len(out["message"]) <= 300
    assert_path_free(out["message"])


def test_worker_check_needs_no_video_and_reports_versions(upstream):
    code, out = run_worker("check", {"backend_dir": str(upstream)})
    assert code == 0 and out["ok"] and out["upstream_importable"] is True and "versions" in out


def test_worker_import_error_and_missing_dir(broken_upstream, tmp_path):
    code, out = run_worker("check", {"backend_dir": str(broken_upstream)})
    assert code == 0 and out["error"] == "import_error"
    assert_path_free(out["message"])
    code, out = run_worker("check", {"backend_dir": str(tmp_path / "nope")})
    assert code == 0 and out["error"] == "import_error"
    assert run_worker("list", {"backend_dir": str(broken_upstream), "video_id": "x"})[1]["error"] == "import_error"


def test_worker_bad_request_json(upstream):
    done = subprocess.run([sys.executable, str(sp.WORKER_PATH), "list"], input=b"{not json",
                          capture_output=True, timeout=60)
    assert done.returncode == 0 and json.loads(done.stdout)["error"] == "internal"


def test_worker_does_not_write_into_upstream_dir(upstream):
    run_worker("list", {"backend_dir": str(upstream), "video_id": "ok"})
    assert not (upstream.parent / "data").exists()


# --- client error classification ---------------------------------------------------------


@pytest.mark.parametrize("video_id,exc", [
    ("blocked", BlockedByProvider), ("nosub", SubtitlesUnavailable), ("lang", LanguageUnavailable),
    ("boom", ProviderUnavailable), ("crash", ProviderUnavailable),
    ("net", ProviderTimeout)])  # transient network error => retried later, not an operator failure
def test_client_maps_errors_without_paths(upstream, video_id, exc):
    client = SubprocessSubtitleClient(cfg(upstream))
    with pytest.raises(exc) as info:
        client.fetch(video_id)
    assert_path_free(str(info.value))


def test_import_error_gives_actionable_install_hint(broken_upstream):
    with pytest.raises(ProviderUnavailable) as info:
        SubprocessSubtitleClient(cfg(broken_upstream)).list_tracks("x")
    assert "pip install -r backend/requirements-subtitle.txt" in str(info.value)
    assert_path_free(str(info.value))


def test_missing_interpreter_is_provider_unavailable(upstream, tmp_path):
    client = SubprocessSubtitleClient(cfg(upstream, python=str(tmp_path / "no-such-python.exe")))
    with pytest.raises(ProviderUnavailable) as info:
        client.list_tracks("ok")
    assert_path_free(str(info.value))


@pytest.mark.parametrize("stdout,returncode", [
    (b"", 0), (b"not json", 0), (b"[1, 2]", 0), (b'{"foo": 1}', 0), (b'{"ok": true}', 3)])
def test_malformed_or_failed_worker_output(upstream, stdout, returncode):
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, returncode, stdout, b"secret C:\\stderr")
    client = SubprocessSubtitleClient(cfg(upstream), runner=runner)
    with pytest.raises(ProviderUnavailable, match="subtitle worker failed"):
        client.list_tracks("ok")


def test_ok_payload_missing_fields_is_worker_failure(upstream):
    def runner(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, b'{"ok": true, "subtitle": {}}', b"")
    with pytest.raises(ProviderUnavailable):
        SubprocessSubtitleClient(cfg(upstream), runner=runner).fetch("v")


def test_runner_receives_hard_timeout_and_request(upstream):
    seen = {}

    def runner(cmd, *, input, timeout, env, cwd):
        seen.update(cmd=cmd, input=json.loads(input), timeout=timeout, env=env, cwd=cwd)
        return subprocess.CompletedProcess(cmd, 0, b'{"ok": true, "tracks": []}', b"")
    SubprocessSubtitleClient(cfg(upstream, timeout=7.5), runner=runner).list_tracks("abc")
    assert seen["timeout"] == 7.5 and seen["cmd"][1:] == [str(sp.WORKER_PATH), "list"]
    assert seen["input"]["video_id"] == "abc" and seen["input"]["backend_dir"] == str(upstream)
    assert seen["env"]["PYTHONDONTWRITEBYTECODE"] == "1" and seen["env"]["PYTHONUTF8"] == "1"


def test_timeout_expired_from_runner_maps_to_provider_timeout(upstream):
    def runner(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    with pytest.raises(ProviderTimeout):
        SubprocessSubtitleClient(cfg(upstream), runner=runner).list_tracks("x")


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_hard_timeout_kills_real_child(upstream, tmp_path, monkeypatch):
    pid_file = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_PID_FILE", str(pid_file))
    client = SubprocessSubtitleClient(cfg(upstream, timeout=4.0))
    with pytest.raises(ProviderTimeout) as info:
        client.list_tracks("sleep")
    assert_path_free(str(info.value))
    assert pid_file.exists(), "child never reached the sleeping fake upstream"
    assert not _pid_alive(int(pid_file.read_text()))


# --- status ---------------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_status_ready_then_cached_then_refreshed(upstream):
    calls = []
    clock = Clock()

    def runner(cmd, **kw):
        calls.append(cmd[-1])
        return sp.run_killing_tree(cmd, **kw)
    client = SubprocessSubtitleClient(cfg(upstream), runner=runner, clock=clock)
    first = client.status()
    assert (first.state, first.kind, first.name) == (READY, "subtitle", "external") and first.usable
    assert client.status() is first and calls == ["check"]
    clock.now += 29
    assert client.status() is first and len(calls) == 1
    clock.now += 2
    client.status()
    assert calls == ["check", "check"]


def test_status_unavailable_variants(broken_upstream, upstream, tmp_path):
    for config in (cfg(broken_upstream), cfg(tmp_path / "missing"),
                   cfg(upstream, python=str(tmp_path / "no-python.exe"))):
        status = SubprocessSubtitleClient(config).status()
        assert status.state == UNAVAILABLE and not status.usable
        assert status.message and len(status.message) < 200
        assert_path_free(status.message)


def test_status_timeout_is_unavailable(upstream):
    def runner(cmd, **kw):
        assert kw["timeout"] == sp.CHECK_TIMEOUT
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    assert SubprocessSubtitleClient(cfg(upstream), runner=runner).status().state == UNAVAILABLE


# --- SourceStep mapping ---------------------------------------------------------------------

NOW = datetime(2026, 1, 2, 12, 0, 0)


class RaisingClient(FakeSubtitleClient):
    def __init__(self, exc):
        super().__init__({})
        self.exc = exc

    def fetch(self, *a, **kw):
        raise self.exc


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def project(db):
    wf = ChannelWorkflow(name="c", mode="auto", status="active", config={
        "source": {"video_id": "vi-video", "languages": ["vi"]},
        "story": {"branch": "what if", "direction": "dark", "target_length": 500}})
    db.add(wf)
    db.commit()
    p = StoryProject(title="My story", channel_workflow_id=wf.id)
    db.add(p)
    db.commit()
    return p


def make_ctx(session_factory, store, client):
    return PipelineContext(session_factory=session_factory, store=store, subtitle_client=client, clock=lambda: NOW)


@pytest.mark.parametrize("exc,status,code", [
    (ProviderUnavailable("x"), StepStatus.FAILED, "provider_unavailable"),
    (ProviderTimeout("x"), StepStatus.NOT_STARTED, "provider_timeout"),
    (BlockedByProvider("x"), StepStatus.NOT_STARTED, "provider_blocked"),
])
def test_source_step_provider_error_mapping(session_factory, store, project, db, exc, status, code):
    view = SourceStep().run(make_ctx(session_factory, store, RaisingClient(exc)), project.id)
    assert (view.status, view.error_code) == (status, code)
    assert db.query(SourceSnapshot).count() == 0  # nothing persisted, next tick can retry


def test_source_step_full_run_over_subprocess_client(session_factory, store, project, db, upstream):
    client = SubprocessSubtitleClient(cfg(upstream))
    view = SourceStep().run(make_ctx(session_factory, store, client), project.id)
    assert view.status is StepStatus.COMPLETED
    snap = db.get(SourceSnapshot, view.domain_id)
    assert snap.content == "Xin chào thế giới\nTạm biệt"
    assert snap.meta["language_code"] == "vi" and snap.meta["provider"] == "SubprocessSubtitleClient"
    assert store.read(snap.meta["artifact_path"]).decode("utf-8") == snap.content
