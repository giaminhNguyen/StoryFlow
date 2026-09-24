"""VieNeu real-TTS backend tests. NEVER loads a real model or touches the network.

The REAL ``vieneu_worker.py`` runs under ``sys.executable`` with a stub ``vieneu`` package injected via
PYTHONPATH (deterministic sine audio, 24 kHz to prove the header is honoured). numpy/soundfile are not
installed in the StoryFlow venv, so this also exercises the worker's stdlib-only PCM/wave path.
Stub behaviour is driven by STUB_* environment variables.
"""

import io
import json
import os
import subprocess
import sys
import threading
import time
import wave
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from storyflow import tts_steps
from storyflow.agents import AgentRunner, RunnerRegistry
from storyflow.artifacts import ArtifactStore
from storyflow.dispatcher import Dispatcher
from storyflow.integrations import vieneu as vn
from storyflow.integrations.vieneu import (
    VieNeuAudioRunner, VieNeuPipelineRunner, VieNeuProvider, pcm_wav_duration_ms, scrub,
)
from storyflow.models import (
    AudioChunk, AudioGeneration, ChannelWorkflow, PipelineJob, RunnerInstance, StoryProject, WorkflowSession,
)
from storyflow.orchestrator import Orchestrator
from storyflow.pipeline import OutputValidatingRunner, PipelineContext
from storyflow.protocol import ResultCode, TaskPacket
from storyflow.providers import READY, UNAVAILABLE, ProviderConfig
from storyflow.roles import Role
from storyflow.story_steps import STORY_VALIDATORS, CanonStep, FakeStoryPipelineRunner, SourceStep, StoryStep
from storyflow.subtitles import FakeSubtitleClient
from storyflow.tts_steps import TTS_VALIDATORS, AudioStep, TTSStep

STUB = '''
import math, os, sys, time

SAMPLE_RATE = 24000
_VOICES = {"Ngọc Huyền", "Thái Sơn", "narrator"}
_calls = [0]


class Vieneu:
    def __init__(self, mode=None, precision="fp32", threads=1):
        mode_err = os.environ.get("STUB_LOAD_ERROR")
        if mode_err == "oom":
            raise MemoryError("out of memory while loading C:\\\\models\\\\secret.bin")
        if mode_err:
            raise RuntimeError("weights corrupt")
        dump = os.environ.get("STUB_ENVDUMP")
        if dump:
            with open(dump, "w", encoding="utf-8") as f:
                f.write(",".join(sorted(k for k in os.environ if k.upper().startswith("STORYFLOW_"))))
        self.sample_rate = SAMPLE_RATE
        print("stray library banner on stdout")

    def list_preset_voices(self):
        return [(v + " - desc", v) for v in sorted(_VOICES)]

    def get_preset_voice(self, name=None):
        if name not in _VOICES:
            raise ValueError(f"Voice '{name}' not found. Available: {sorted(_VOICES)}")
        return {}

    def infer(self, text, voice=None, temperature=0.4, **kw):
        print("progress 50% (stray print)")
        pid =os.environ.get("STUB_PIDFILE")
        if pid:
            with open(pid, "w") as f:
                f.write(str(os.getpid()))
        if os.environ.get("STUB_SLEEP"):
            time.sleep(float(os.environ["STUB_SLEEP"]))
        if os.environ.get("STUB_CRASH"):
            sys.stderr.write("Traceback: boom at C:\\\\secret\\\\dir\\\\x.py token=abc123 /home/u/priv/file\\n")
            sys.stderr.flush()
            os._exit(7)
        after = os.environ.get("STUB_FAIL_AFTER")
        if after is not None and _calls[0] >= int(after):
            once = os.environ.get("STUB_ONCE_FLAG")
            if not once or not os.path.exists(once):
                if once:
                    open(once, "w").close()
                raise RuntimeError("synthesis blew up for C:\\\\secret\\\\spot.txt")
        log = os.environ.get("STUB_LOG")
        if log:
            with open(log, "a", encoding="utf-8") as f:
                f.write(voice + "|" + str(len(text)) + "\\n")
        _calls[0] += 1
        n = SAMPLE_RATE // 20 * max(1, len(text) // 10)
        return [0.5 * math.sin(2 * math.pi * 220 * i / SAMPLE_RATE) for i in range(n)]
'''

TASK = "task-1"
OUT_DIR = "projects/p1/audio/t1/run-001"
CHUNK_DIR = "projects/p1/tts/t1/chunks"


@pytest.fixture
def stub(tmp_path, monkeypatch):
    d = tmp_path / "stubpkg"
    d.mkdir()
    (d / "vieneu.py").write_text(STUB, encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(d))
    for k in list(os.environ):
        if k.startswith("STUB_"):
            monkeypatch.delenv(k)
    return d


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "artifacts")


def cfg(**kw):
    base = dict(tts_engine="vieneu", vieneu_python=sys.executable, vieneu_voice="Ngọc Huyền", tts_timeout=60.0)
    base.update(kw)
    return ProviderConfig(**base)


def make_packet(store, texts, voice="default", task_id=TASK):
    chunks, outputs = [], []
    for i, t in enumerate(texts, 1):
        store.write(f"{CHUNK_DIR}/{i:04d}.txt", (t + "\n").encode("utf-8"))
        chunks.append(f"{CHUNK_DIR}/{i:04d}.txt")
        outputs.append(f"{OUT_DIR}/{i:04d}.wav")
    return TaskPacket(task_id=task_id, role=Role.TTS_ADAPTER.value,
                      inputs={"chunks": chunks, "output_dir": OUT_DIR}, outputs=outputs,
                      task_config={"step": "audio", "voice": voice})


TEXTS = ["Chuong mot bat dau day.", "Chuong hai tiep noi phia sau.", "Chuong ba ket thuc cau chuyen."]


def leftovers(store):
    return [p.name for p in store.root.rglob("*") if p.name.endswith(".part") or p.name.startswith(".tmp-")]


def pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_for(path, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and path.read_text().strip():
            return
        time.sleep(0.05)
    raise AssertionError("file never appeared")


# --- wav helper --------------------------------------------------------------


def test_pcm_wav_duration_accepts_real_and_rejects_garbage():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(48000)
        w.writeframes(b"\x01\x00" * 48000)
    assert pcm_wav_duration_ms(buf.getvalue()) == 1000
    assert pcm_wav_duration_ms(tts_steps.fake_wav("hello")) == 100  # the fake 8 kHz format still passes
    assert pcm_wav_duration_ms(b"") is None and pcm_wav_duration_ms(b"garbage" * 20) is None
    assert pcm_wav_duration_ms(buf.getvalue()[:-1000]) is None  # truncated payload


# --- runner: success / skip / invalid ----------------------------------------


def test_full_success_writes_valid_wavs_in_order(stub, store, monkeypatch, tmp_path):
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "log.txt"))
    runner = VieNeuAudioRunner(cfg(), store)
    packet = make_packet(store, TEXTS, voice="narrator")
    res = runner.execute(packet)
    assert res.code is ResultCode.SUCCESS, res.error_message
    assert res.metrics["produced"] == 3 and res.metrics["skipped"] == 0 and res.metrics["duration_ms"] > 0
    assert set(res.metrics) == {"produced", "skipped", "duration_ms"}
    for out in packet.outputs:
        data = store.read(out)
        assert data[:4] == b"RIFF" and pcm_wav_duration_ms(data) > 0
        with wave.open(io.BytesIO(data)) as w:
            assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24000, 1, 2)
    log = (tmp_path / "log.txt").read_text(encoding="utf-8").splitlines()
    assert [line.split("|")[0] for line in log] == ["narrator"] * 3
    assert not leftovers(store)


def test_voice_falls_back_to_config_when_packet_says_default(stub, store, monkeypatch, tmp_path):
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "log.txt"))
    runner = VieNeuAudioRunner(cfg(vieneu_voice="Thái Sơn"), store)
    assert runner.execute(make_packet(store, TEXTS[:1], voice="default")).code is ResultCode.SUCCESS
    assert (tmp_path / "log.txt").read_text(encoding="utf-8").startswith("Thái Sơn|")


def test_valid_existing_chunks_are_skipped_and_untouched(stub, store, monkeypatch, tmp_path):
    runner = VieNeuAudioRunner(cfg(), store)
    packet = make_packet(store, TEXTS)
    assert runner.execute(packet).code is ResultCode.SUCCESS
    files = [store.resolve(o) for o in packet.outputs]
    before = [(f.read_bytes(), f.stat().st_mtime_ns) for f in files]
    # second run must not even spawn a worker: point the interpreter at nothing
    res = VieNeuAudioRunner(cfg(vieneu_python=str(tmp_path / "nope.exe")), store).execute(packet)
    assert res.code is ResultCode.SUCCESS and res.metrics == {"produced": 0, "skipped": 3, "duration_ms": 0}
    assert [(f.read_bytes(), f.stat().st_mtime_ns) for f in files] == before


def test_only_missing_chunks_are_synthesized(stub, store, monkeypatch, tmp_path):
    runner = VieNeuAudioRunner(cfg(), store)
    packet = make_packet(store, TEXTS)
    assert runner.execute(packet).code is ResultCode.SUCCESS
    keep, drop = store.resolve(packet.outputs[0]), store.resolve(packet.outputs[1])
    kept = (keep.read_bytes(), keep.stat().st_mtime_ns)
    drop.unlink()
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "log.txt"))
    res = runner.execute(packet)
    assert res.code is ResultCode.SUCCESS and res.metrics["produced"] == 1 and res.metrics["skipped"] == 2
    assert (keep.read_bytes(), keep.stat().st_mtime_ns) == kept
    assert len((tmp_path / "log.txt").read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.parametrize("bad", [b"", b"not-a-wav-at-all" * 10])
def test_invalid_existing_chunk_is_replaced_atomically(stub, store, bad):
    runner = VieNeuAudioRunner(cfg(), store)
    packet = make_packet(store, TEXTS[:2])
    store.write(packet.outputs[0], bad)
    res = runner.execute(packet)
    assert res.code is ResultCode.SUCCESS and res.metrics["produced"] == 2
    assert pcm_wav_duration_ms(store.read(packet.outputs[0])) > 0
    assert not leftovers(store)


def test_long_chunk_is_split_into_pieces(stub, store, monkeypatch, tmp_path):
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "log.txt"))
    sentence = "Cau chuyen nay rat dai va cu tiep tuc mai. "
    text = (sentence * 40).strip()  # ~1700 chars, one paragraph
    res = VieNeuAudioRunner(cfg(), store).execute(make_packet(store, [text]))
    assert res.code is ResultCode.SUCCESS
    sizes = [int(line.split("|")[1]) for line in (tmp_path / "log.txt").read_text(encoding="utf-8").splitlines()]
    assert len(sizes) >= 3 and max(sizes) <= 700


# --- partial failure ---------------------------------------------------------


def test_partial_failure_keeps_finished_chunks_and_retry_resumes(stub, store, monkeypatch, tmp_path):
    runner = VieNeuAudioRunner(cfg(), store)
    packet = make_packet(store, TEXTS)
    monkeypatch.setenv("STUB_FAIL_AFTER", "1")
    res = runner.execute(packet)
    assert res.code is ResultCode.TASK_FAILED and res.error_code == "partial_failure"
    assert res.metrics["produced"] == 1
    assert "0002.wav" in res.error_message and "secret" not in res.error_message and "C:" not in res.error_message
    first = store.resolve(packet.outputs[0])
    kept = (first.read_bytes(), first.stat().st_mtime_ns)
    assert pcm_wav_duration_ms(kept[0]) > 0
    assert not store.exists(packet.outputs[1]) and not store.exists(packet.outputs[2])
    assert not leftovers(store)
    monkeypatch.delenv("STUB_FAIL_AFTER")
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "log.txt"))
    res = runner.execute(packet)
    assert res.code is ResultCode.SUCCESS and res.metrics["produced"] == 2 and res.metrics["skipped"] == 1
    assert (first.read_bytes(), first.stat().st_mtime_ns) == kept  # byte-identical, never rewritten
    assert len((tmp_path / "log.txt").read_text(encoding="utf-8").splitlines()) == 2
    assert not leftovers(store)


def test_failure_on_first_chunk_leaves_no_part_file(stub, store, monkeypatch):
    monkeypatch.setenv("STUB_FAIL_AFTER", "0")
    res = VieNeuAudioRunner(cfg(), store).execute(make_packet(store, TEXTS))
    assert res.code is ResultCode.TASK_FAILED and res.error_code == "partial_failure"
    assert res.metrics["produced"] == 0 and not leftovers(store)
    assert not list(store.root.rglob("*.wav"))


# --- other failure mappings --------------------------------------------------


def test_voice_not_found_is_business_failure(stub, store):
    res = VieNeuAudioRunner(cfg(), store).execute(make_packet(store, TEXTS, voice="Ma Lai"))
    assert res.code is ResultCode.TASK_FAILED and res.error_code == "voice_not_found"
    assert "Ma Lai" in res.error_message and "narrator" not in res.error_message
    assert not list(store.root.rglob("*.wav")) and not leftovers(store)


def test_model_load_and_oom_are_transient(stub, store, monkeypatch):
    monkeypatch.setenv("STUB_LOAD_ERROR", "oom")
    res = VieNeuAudioRunner(cfg(), store).execute(make_packet(store, TEXTS))
    assert res.code is ResultCode.TRANSIENT_FAILURE and res.error_code == "oom"
    assert "secret" not in res.error_message and "C:" not in res.error_message
    monkeypatch.setenv("STUB_LOAD_ERROR", "load")
    res = VieNeuAudioRunner(cfg(), store).execute(make_packet(store, TEXTS))
    assert res.code is ResultCode.TRANSIENT_FAILURE and res.error_code == "model_load"


def test_worker_crash_without_json_is_runner_crashed_and_scrubbed(stub, store, monkeypatch):
    monkeypatch.setenv("STUB_CRASH", "1")
    res = VieNeuAudioRunner(cfg(), store).execute(make_packet(store, TEXTS))
    assert res.code is ResultCode.RUNNER_CRASHED and res.error_code == "worker_crash"
    for leaked in ("secret", "abc123", "C:\\", "/home/u"):
        assert leaked not in res.error_message
    assert len(res.error_message) <= 300 and not leftovers(store)


def test_missing_interpreter_is_runner_crashed(store, tmp_path):
    runner = VieNeuAudioRunner(cfg(vieneu_python=str(tmp_path / "missing" / "python.exe")), store)
    res = runner.execute(make_packet(store, TEXTS))
    assert res.code is ResultCode.RUNNER_CRASHED and res.error_code == "vieneu_missing"
    assert str(tmp_path) not in res.error_message
    res = VieNeuAudioRunner(cfg(vieneu_python=None, vieneu_root=None), store).execute(make_packet(store, TEXTS))
    assert res.code is ResultCode.RUNNER_CRASHED


def test_spawn_oserror_is_runner_crashed(stub, store, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise PermissionError(f"denied {tmp_path}")
    monkeypatch.setattr(vn, "_popen", boom)
    res = VieNeuAudioRunner(cfg(), store).execute(make_packet(store, TEXTS))
    assert res.code is ResultCode.RUNNER_CRASHED and str(tmp_path) not in res.error_message


def test_timeout_kills_the_process_tree(stub, store, monkeypatch, tmp_path):
    pidfile = tmp_path / "pid.txt"
    monkeypatch.setenv("STUB_PIDFILE", str(pidfile))
    monkeypatch.setenv("STUB_SLEEP", "60")
    start = time.time()
    res = VieNeuAudioRunner(cfg(tts_timeout=2.0), store).execute(make_packet(store, TEXTS))
    assert res.code is ResultCode.TIMEOUT and res.error_code == "timeout"
    assert time.time() - start < 30
    pid = int(pidfile.read_text())
    time.sleep(0.5)
    assert not pid_alive(pid), "worker orphaned after timeout"
    assert not leftovers(store)


def test_cancel_kills_worker(stub, store, monkeypatch, tmp_path):
    pidfile = tmp_path / "pid.txt"
    monkeypatch.setenv("STUB_PIDFILE", str(pidfile))
    monkeypatch.setenv("STUB_SLEEP", "60")
    runner = VieNeuAudioRunner(cfg(), store)
    holder = {}
    t = threading.Thread(target=lambda: holder.update(res=runner.execute(make_packet(store, TEXTS))))
    t.start()
    wait_for(pidfile)
    assert runner.cancel("nope") is False
    assert runner.cancel(TASK) is True
    t.join(30)
    assert not t.is_alive()
    assert holder["res"].code is ResultCode.CANCELLED
    time.sleep(0.5)
    assert not pid_alive(int(pidfile.read_text()))
    assert not leftovers(store) and TASK not in runner._procs


# --- packet validation / environment -----------------------------------------


@pytest.mark.parametrize("bad_out", ["../evil.wav", "/abs/evil.wav", "C:/abs/evil.wav", "projects/p1/other/x.wav",
                                     "notprojects/x.wav"])
def test_packet_output_paths_are_validated(stub, store, bad_out):
    packet = make_packet(store, TEXTS[:1])
    bad = TaskPacket(task_id=TASK, inputs=packet.inputs, outputs=[bad_out], task_config=packet.task_config)
    with pytest.raises((ValueError,)):
        VieNeuAudioRunner(cfg(), store).execute(bad)


def test_packet_chunk_traversal_and_count_mismatch_rejected(stub, store):
    packet = make_packet(store, TEXTS[:1])
    bad = TaskPacket(task_id=TASK, inputs={**packet.inputs, "chunks": ["projects/../../etc/x.txt"]},
                     outputs=packet.outputs, task_config=packet.task_config)
    with pytest.raises(ValueError):
        VieNeuAudioRunner(cfg(), store).execute(bad)
    mismatch = TaskPacket(task_id=TASK, inputs={**packet.inputs, "chunks": packet.inputs["chunks"] * 2},
                          outputs=packet.outputs, task_config=packet.task_config)
    with pytest.raises(ValueError):
        VieNeuAudioRunner(cfg(), store).execute(mismatch)


def test_wrong_step_rejected(stub, store):
    packet = make_packet(store, TEXTS[:1])
    with pytest.raises(ValueError):
        VieNeuAudioRunner(cfg(), store).execute(
            TaskPacket(task_id=TASK, inputs=packet.inputs, outputs=packet.outputs, task_config={"step": "tts"}))


def test_worker_env_has_no_storyflow_vars(stub, store, monkeypatch, tmp_path):
    monkeypatch.setenv("STORYFLOW_DATABASE_URL", "sqlite:///x")
    monkeypatch.setenv("STORYFLOW_SECRET_TOKEN", "hunter2")
    dump = tmp_path / "env.txt"
    monkeypatch.setenv("STUB_ENVDUMP", str(dump))
    assert VieNeuAudioRunner(cfg(), store).execute(make_packet(store, TEXTS[:1])).code is ResultCode.SUCCESS
    assert dump.read_text() == ""
    env = vn.clean_env({"STORYFLOW_X": "1", "storyflow_y": "2", "PATH": "p"})
    assert "STORYFLOW_X" not in env and "storyflow_y" not in env and env["PATH"] == "p"


# --- protocol parsing / scrubbing --------------------------------------------


def test_read_events_ignores_garbage_and_oversized_lines():
    big = b"x" * (vn.MAX_LINE * 3)
    raw = (b'{"event":"chunk","index":1}\n' + b"garbage line\n" + b"\xff\xfe\n" + b"[1,2]\n" + b"\n" + big + b"\n" +
           b'{"event":"done","made":1}\n' + b'{"event":"tail-without-newline"}')
    events = []
    vn._read_events(io.BytesIO(raw), events)
    assert [e["event"] for e in events] == ["chunk", "done", "tail-without-newline"]


def test_scrub_bounds_and_removes_paths_and_secrets():
    text = "failed C:\\Users\\bob\\file.txt and /home/bob/x/y token=abc sk-ABCDEFGH12345 " + "z" * 500
    out = scrub(text, "E:\\OTHER")
    assert len(out) <= 300
    for leaked in ("bob", "abc", "ABCDEFGH", "C:\\"):
        assert leaked not in out
    assert "<path>" in scrub("under E:\\OTHER\\root", "E:\\OTHER")


# --- provider ----------------------------------------------------------------


def test_provider_status_ready_and_detect(stub, store):
    p = VieNeuProvider(cfg(), store)
    st = p.status()
    assert st.state == READY and st.kind == "tts" and st.name == "vieneu" and st.details["engine"]
    (det,) = p.detect()
    assert det.runner_id == "vieneu-1" and det.health.ok and det.health.state == "ready"
    assert p.name == "vieneu" and p.roles == [Role.TTS_ADAPTER.value] and p.max_concurrency == 1
    assert isinstance(p.build("vieneu-1"), VieNeuPipelineRunner)


def test_provider_unavailable_when_python_missing_is_actionable_and_path_free(store, tmp_path):
    p = VieNeuProvider(cfg(vieneu_python=str(tmp_path / "gone" / "python.exe")), store)
    st = p.status()
    assert st.state == UNAVAILABLE and "STORYFLOW_VIENEU_ROOT" in st.message and str(tmp_path) not in st.message
    assert p.detect()[0].health.ok is False
    st = VieNeuProvider(cfg(vieneu_python=None, vieneu_root=str(tmp_path)), store).status()
    assert st.state == UNAVAILABLE


def test_provider_unavailable_when_vieneu_not_importable(store, monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)
    st = VieNeuProvider(cfg(), store).status()
    assert st.state == UNAVAILABLE and "not importable" in st.message and "STORYFLOW" not in st.message.split(":")[0]


def test_provider_check_is_cached_for_60s(stub, store):
    now = [1000.0]
    p = VieNeuProvider(cfg(), store, monotonic=lambda: now[0])
    calls = []
    real = p._check_uncached
    p._check_uncached = lambda: (calls.append(1), real())[1]
    p.status()
    p.detect()
    now[0] += 59
    p.status()
    assert len(calls) == 1
    now[0] += 2
    p.status()
    assert len(calls) == 2


def test_check_does_not_need_the_model(stub, store, monkeypatch):
    monkeypatch.setenv("STUB_LOAD_ERROR", "load")  # constructing Vieneu would fail; check must not construct it
    assert VieNeuProvider(cfg(), store).status().state == READY


# --- full pipeline through the real Dispatcher --------------------------------

NOW = datetime(2026, 3, 1, 9, 0, 0)
TRACK = {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True,
         "snippets": [{"text": "The hero wakes.", "start": 0.0}, {"text": "The rival waits.", "start": 2.0}]}
ROLES = [Role.STORY_WRITER.value, Role.TTS_ADAPTER.value]
VALIDATORS = {**STORY_VALIDATORS, **TTS_VALIDATORS}


@pytest.fixture
def real_wav_validation():
    """Kept as a no-op fixture name: tts_steps.wav_duration_ms now accepts real mono PCM wavs natively."""
    yield


class StoryPlusVieNeu(AgentRunner):
    """One physical runner: story/canon via the deterministic story fake, tts/audio via VieNeu."""

    runner_type = "vieneu"

    def __init__(self, store, config):
        self.story = FakeStoryPipelineRunner(store)
        self.vieneu = VieNeuPipelineRunner(config, store)
        # small chunks so the fake story yields several chunks
        self.vieneu.tts.chunking = {"preferred_chunk_chars_max": 60, "avoid_chunk_below_chars": 10}

    def execute(self, packet):
        step = packet.task_config["step"]
        return (self.vieneu if step in ("tts", "audio") else self.story).execute(packet)

    def classify_error(self, error):
        return self.vieneu.classify_error(error)


class PipeEnv:
    def __init__(self, session_factory, tmp_path, config):
        self.sf = session_factory
        self.store = ArtifactStore(tmp_path / "artifacts")
        self.ctx = PipelineContext(session_factory=session_factory, store=self.store,
                                   subtitle_client=FakeSubtitleClient({"vid": {"tracks": [TRACK]}}),
                                   clock=lambda: NOW)
        db = session_factory()
        sess = WorkflowSession(mode="auto", status="active", role_preferences={})
        db.add(sess)
        db.commit()
        wf = ChannelWorkflow(workflow_session_id=sess.id, name="chan", mode="auto", config={
            "source": {"video_id": "vid", "languages": ["en"]}, "story": {"branch": "a darker turn"},
            "tts": {"voice": "narrator"}})
        db.add(wf)
        db.commit()
        db.add(StoryProject(channel_workflow_id=wf.id, title="Tale", slug="tale"))
        db.commit()
        self.workflow_id = wf.id
        inst = RunnerInstance(workflow_session_id=sess.id, runner_type="vieneu", max_concurrency=1,
                              supported_roles=ROLES)
        db.add(inst)
        db.commit()
        db.close()
        self.registry = RunnerRegistry()
        self.registry.register(inst.id, OutputValidatingRunner(StoryPlusVieNeu(self.store, config), self.store,
                                                               VALIDATORS))

    def run(self):
        orch = Orchestrator(self.ctx, Dispatcher(self.registry), SourceStep(),
                            [CanonStep(), StoryStep(), TTSStep(), AudioStep()])
        return orch.run_until_idle(self.workflow_id)

    def q(self, fn):
        db = self.sf()
        try:
            return fn(db)
        finally:
            db.close()


def test_pipeline_end_to_end_over_real_worker(stub, session_factory, tmp_path, monkeypatch, real_wav_validation):
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "log.txt"))
    env = PipeEnv(session_factory, tmp_path, cfg())
    assert env.run().status == "completed"
    chunks = env.q(lambda db: db.scalars(select(AudioChunk).order_by(AudioChunk.chunk_index)).all())
    (gen,) = env.q(lambda db: db.scalars(select(AudioGeneration)).all())
    assert len(chunks) >= 2 and [c.chunk_index for c in chunks] == list(range(1, len(chunks) + 1))
    assert gen.status == "completed" and gen.chunk_count == len(chunks)
    assert all(c.duration_ms > 0 and env.store.exists(c.artifact_path) for c in chunks)
    assert all(not Path(c.artifact_path).is_absolute() for c in chunks)
    voices = {line.split("|")[0] for line in (tmp_path / "log.txt").read_text(encoding="utf-8").splitlines()}
    assert voices == {"narrator"}  # the TTSGeneration voice reached the worker
    assert not leftovers(env.store)


def test_pipeline_partial_failure_then_resume_without_duplicates(stub, session_factory, tmp_path, monkeypatch,
                                                                 real_wav_validation):
    monkeypatch.setenv("STUB_FAIL_AFTER", "2")
    monkeypatch.setenv("STUB_ONCE_FLAG", str(tmp_path / "once.flag"))
    monkeypatch.setenv("STUB_LOG", str(tmp_path / "log.txt"))
    env = PipeEnv(session_factory, tmp_path, cfg())
    assert env.run().status == "completed"
    job = env.q(lambda db: next(j for j in db.scalars(select(PipelineJob)) if j.kind == "audio_generation"))
    assert job.attempts == 1 and job.infrastructure_failures == 0  # a business attempt, not infra
    chunks = env.q(lambda db: db.scalars(select(AudioChunk).order_by(AudioChunk.chunk_index)).all())
    assert len(chunks) > 2 and [c.chunk_index for c in chunks] == list(range(1, len(chunks) + 1))
    assert len(env.q(lambda db: db.scalars(select(AudioGeneration)).all())) == 1
    # every chunk was synthesized exactly once across both attempts (2 before the failure, the rest after)
    calls = (tmp_path / "log.txt").read_text(encoding="utf-8").splitlines()
    assert len(calls) == len(chunks)
    assert not leftovers(env.store)
