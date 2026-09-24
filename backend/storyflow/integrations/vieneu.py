"""Real TTS synthesis through the locally installed VieNeu-TTS (Phase 8, workstream C).

Isolation: VieNeu (torch/onnx...) lives in ITS OWN venv. StoryFlow only ever spawns
``[<vieneu python>, integrations/vieneu_worker.py, synth|check]`` and talks JSON lines over pipes, so no
ML dependency can enter the StoryFlow environment. The worker gets absolute paths for ONE call (resolved
from the ArtifactStore, never persisted), a temp cwd and an environment without ``STORYFLOW_*``.

Steps served (``task_config["step"]``):
  * ``audio`` -> :class:`VieNeuAudioRunner` (real synthesis, AudioStep semantics: deterministic chunk
    order, skip-if-valid-exists, a valid chunk is never rewritten, partial failure keeps finished chunks,
    every wav is written ``.part`` -> os.replace by the worker).
  * ``tts``   -> :class:`VieNeuTtsRunner`. The TTS *adaptation* (story_tts.txt + manifest + chunks) is
    RULE-BASED and deterministic per pinned profile - it reuses ``FakeTTSAdapterRunner``'s logic
    (whitespace/markdown normalisation + profile chunker). It is NOT an LLM pass.

Known gap in files owned elsewhere: ``tts_steps.wav_duration_ms`` only accepts the fake 8 kHz/8-bit wav,
so ``validate_audio_output`` / ``sync_chunks`` reject real 48 kHz PCM16 output until it is generalised;
:func:`pcm_wav_duration_ms` is a drop-in replacement (mono PCM 8/16-bit, any rate).
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

from ..agents import AgentRunner
from ..artifacts import ArtifactStore
from ..gateway import DetectedRunner
from ..protocol import ResultCode, RunnerHealth, RunnerResult, TaskPacket
from ..providers import READY, UNAVAILABLE, ProviderConfig, ProviderStatus
from ..roles import Role
from ..runtime.supervisor import RunnerProvider
from ..tts_steps import DEFAULT_VOICE, FakeTTSAdapterRunner, _check_output_path

WORKER_PATH = Path(__file__).with_name("vieneu_worker.py")
RUNNER_TYPE = "vieneu"
RUNNER_ID = "vieneu-1"
ENGINE = "vieneu-tts-v3-turbo"
MSG_LIMIT = 300
MAX_LINE = 64 * 1024          # bounded protocol line; longer lines are dropped
MAX_EVENTS = 100_000
HEALTH_TTL = 60.0
CHECK_TIMEOUT = 30.0
_STDERR_TAIL = 2000

_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/][^\s'\"]*|\\\\[^\s'\"]+|/(?:[\w.\-]+/)+[\w.\-]*)")
_SECRET_RE = re.compile(r"(?i)(?:(?:token|secret|password|passwd|api[_-]?key|authorization)\s*[=:]\s*\S+|sk-[A-Za-z0-9_\-]{8,}|Bearer\s+\S+)")


def scrub(text, *literals: str) -> str:
    """Bounded, path-free, secret-free single line."""
    out = str(text)
    for lit in literals:
        if lit:
            out = out.replace(lit, "<path>")
    out = _SECRET_RE.sub("<redacted>", _PATH_RE.sub("<path>", out))
    return " ".join(out.split())[:MSG_LIMIT]


def pcm_wav_duration_ms(data: bytes) -> int | None:
    """duration_ms of a well-formed non-empty mono PCM wav (8/16-bit, any rate), else None."""
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            frames, rate = w.getnframes(), w.getframerate()
            if (frames <= 0 or rate <= 0 or w.getnchannels() != 1 or w.getsampwidth() not in (1, 2)
                    or w.getcomptype() != "NONE"):
                return None
            if len(data) < 44 + frames * w.getsampwidth():
                return None
            return frames * 1000 // rate
    except (EOFError, wave.Error):
        return None


def clean_env(environ=None) -> dict:
    env = {k: v for k, v in (os.environ if environ is None else environ).items()
           if not k.upper().startswith("STORYFLOW_")}
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the worker and everything it spawned."""
    if proc.poll() is not None:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=15,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _remove_part(path: str) -> None:
    for attempt in range(10):  # Windows may hold the handle for a moment after a tree kill
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except OSError:
            time.sleep(0.05 * (attempt + 1))


def _popen(cmd: list[str], *, cwd: str, stderr) -> subprocess.Popen:
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, cwd=cwd,
                            env=clean_env(), **kwargs)


def parse_event_line(raw: bytes):
    """One protocol line -> dict, or None for garbage / non-objects / oversized lines."""
    if not raw or len(raw) > MAX_LINE:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def _read_events(stream, events: list) -> None:
    """Bounded JSON-lines reader: lines over MAX_LINE are drained and ignored, garbage is skipped."""
    while True:
        try:
            raw = stream.readline(MAX_LINE + 1)
        except (OSError, ValueError):
            return
        if not raw:
            return
        if len(raw) > MAX_LINE and not raw.endswith(b"\n"):
            while True:  # drain the rest of the oversized line
                try:
                    more = stream.readline(MAX_LINE + 1)
                except (OSError, ValueError):
                    return
                if not more or more.endswith(b"\n"):
                    break
            continue
        event = parse_event_line(raw.strip())
        if event is not None and len(events) < MAX_EVENTS:
            events.append(event)


class VieNeuAudioRunner(AgentRunner):
    runner_type = RUNNER_TYPE

    def __init__(self, config: ProviderConfig, store: ArtifactStore, *, worker_path: Path | None = None):
        self.config = config
        self.store = store
        self.worker_path = Path(worker_path) if worker_path else WORKER_PATH
        self._procs: dict[str, subprocess.Popen] = {}
        self._cancelled: set[str] = set()
        self._lock = threading.Lock()

    # -- helpers -------------------------------------------------------------------------------

    def _voice(self, packet: TaskPacket) -> str:
        for source in (packet.task_config or {}, packet.inputs or {}):
            v = source.get("voice")
            if isinstance(v, str) and v.strip() and v.strip() != DEFAULT_VOICE:
                return v.strip()
        return self.config.vieneu_voice

    def _valid(self, rel: str) -> int | None:
        if not self.store.exists(rel):
            return None
        return pcm_wav_duration_ms(self.store.read(rel))

    def classify_error(self, error):
        return ResultCode.TIMEOUT if isinstance(error, TimeoutError) else ResultCode.RUNNER_CRASHED

    def health(self) -> RunnerHealth:
        return RunnerHealth(ok=self.config.resolved_vieneu_python() is not None, runner_type=RUNNER_TYPE)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(task_id)
            if proc is None:
                return False
            self._cancelled.add(task_id)
        kill_tree(proc)
        return True

    @staticmethod
    def _fail(code, error_code, message, metrics=None, *literals) -> RunnerResult:
        return RunnerResult(code=code, error_code=error_code, error_message=scrub(message, *literals),
                            metrics=metrics)

    # -- execute -------------------------------------------------------------------------------

    def execute(self, packet: TaskPacket) -> RunnerResult:
        step = (packet.task_config or {}).get("step")
        if step != "audio":
            raise ValueError(f"VieNeuAudioRunner only serves step 'audio', got {step!r}")
        out_dir = packet.inputs["output_dir"]
        chunks = list(packet.inputs["chunks"])
        outputs = list(packet.outputs)
        if len(chunks) != len(outputs):
            raise ValueError("outputs must list one audio file per chunk")
        for p in outputs:
            _check_output_path(self.store, p, out_dir)
        for c in chunks:
            _check_output_path(self.store, c)

        pending = [(c, o) for c, o in zip(chunks, outputs) if self._valid(o) is None]
        already = len(outputs) - len(pending)
        if not pending:
            return RunnerResult(code=ResultCode.SUCCESS,
                                metrics={"produced": 0, "skipped": already, "duration_ms": 0})
        python = self.config.resolved_vieneu_python()
        if python is None or not Path(python).is_file():
            return self._fail(ResultCode.RUNNER_CRASHED, "vieneu_missing",
                              "VieNeu interpreter not found; check STORYFLOW_VIENEU_ROOT / STORYFLOW_VIENEU_PYTHON")
        voice = self._voice(packet)
        request = {
            "voice": voice, "precision": self.config.vieneu_precision, "threads": self.config.vieneu_threads,
            "temperature": 0.8, "gap_seconds": 0.35,
            "items": [{"text_file": str(self.store.resolve(c)), "out_file": str(self.store.resolve(o))}
                      for c, o in pending],
        }
        names = [Path(o).name for _c, o in pending]
        return self._run(packet.task_id or "", python, request, voice, names, already)

    def _run(self, task_id, python, request, voice, names, already) -> RunnerResult:
        secret_paths = (str(self.store.root), str(python), str(self.worker_path))
        events: list = []
        with tempfile.TemporaryDirectory(prefix="storyflow-vieneu-") as tmp:
            err_path = Path(tmp) / "stderr.log"
            try:
                with open(err_path, "wb") as err:
                    proc = _popen([str(python), str(self.worker_path), "synth"], cwd=tmp, stderr=err)
            except OSError as exc:
                return self._fail(ResultCode.RUNNER_CRASHED, "vieneu_spawn_failed",
                                  f"cannot start the VieNeu worker: {type(exc).__name__}", None, *secret_paths)
            with self._lock:
                self._procs[task_id] = proc
                self._cancelled.discard(task_id)
            reader = threading.Thread(target=_read_events, args=(proc.stdout, events), daemon=True)
            reader.start()
            timed_out = False
            try:
                try:
                    proc.stdin.write(json.dumps(request).encode("utf-8"))
                    proc.stdin.close()
                except OSError:
                    pass  # worker died early; handled through the exit path
                try:
                    proc.wait(timeout=self.config.tts_timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    kill_tree(proc)
                    proc.wait()
            finally:
                kill_tree(proc)  # no-op when exited; guarantees no orphan on any exit path
                reader.join(timeout=10)
                try:
                    proc.stdout.close()
                except OSError:
                    pass
                with self._lock:
                    self._procs.pop(task_id, None)
                    cancelled = task_id in self._cancelled
                    self._cancelled.discard(task_id)
            for item in request["items"]:  # a killed/crashed worker cannot clean its own .part files
                _remove_part(item["out_file"] + ".part")
            try:
                tail = err_path.read_bytes()[-_STDERR_TAIL:].decode("utf-8", errors="replace")
            except OSError:
                tail = ""
        return self._result(events, proc.returncode, timed_out, cancelled, voice, names, already, tail,
                            secret_paths)

    def _result(self, events, returncode, timed_out, cancelled, voice, names, already, stderr_tail, literals):
        chunk_ev = [e for e in events if e.get("event") == "chunk"]
        skip_ev = [e for e in events if e.get("event") == "skip"]
        produced = len(chunk_ev)
        skipped = already + len(skip_ev)
        duration = sum(e["duration_ms"] for e in chunk_ev if isinstance(e.get("duration_ms"), int))
        metrics = {"produced": produced, "skipped": skipped, "duration_ms": duration}
        if cancelled:
            return self._fail(ResultCode.CANCELLED, "cancelled", "audio synthesis cancelled", metrics)
        if timed_out:
            return self._fail(ResultCode.TIMEOUT, "timeout",
                              f"VieNeu synthesis exceeded {self.config.tts_timeout:g}s and was killed", metrics)
        error = next((e for e in reversed(events) if e.get("event") == "error"), None)
        if error is not None:
            kind = str(error.get("kind") or "synth_failed")
            msg = scrub(error.get("message") or kind, *literals)
            if kind == "voice_not_found":
                return self._fail(ResultCode.TASK_FAILED, "voice_not_found",
                                  f"voice '{voice[:60]}' was not found in VieNeu; configure an available preset voice",
                                  metrics)
            if kind in ("oom", "model_load"):
                return self._fail(ResultCode.TRANSIENT_FAILURE, kind, f"VieNeu {kind}: {msg}", metrics)
            if kind in ("import_error", "bad_request"):
                return self._fail(ResultCode.RUNNER_CRASHED, f"vieneu_{kind}", f"VieNeu worker: {msg}", metrics)
            idx = error.get("index")
            where = f" at {names[idx - 1]}" if isinstance(idx, int) and 1 <= idx <= len(names) else ""
            return self._fail(ResultCode.TASK_FAILED, "partial_failure",
                              f"synthesis stopped{where} after {produced} new chunk(s) ({kind}): {msg}", metrics)
        if returncode == 0 and any(e.get("event") == "done" for e in events):
            return RunnerResult(code=ResultCode.SUCCESS, metrics=metrics)
        detail = scrub(stderr_tail.strip().splitlines()[-1], *literals) if stderr_tail.strip() else ""
        return self._fail(ResultCode.RUNNER_CRASHED, "worker_crash",
                          f"VieNeu worker exited with code {returncode} without a result" +
                          (f": {detail}" if detail else ""), metrics)


class VieNeuTtsRunner(FakeTTSAdapterRunner):
    """TTS adaptation step (story_tts.txt + manifest + chunks). Rule-based and deterministic per the pinned
    profile - identical logic to ``FakeTTSAdapterRunner`` (not an LLM pass); only the runner identity differs."""

    def __init__(self, store: ArtifactStore):
        super().__init__(store, runner_type=RUNNER_TYPE)


class VieNeuPipelineRunner(AgentRunner):
    """Routes ``task_config["step"]``: ``tts`` -> rule-based adapter, ``audio`` -> real VieNeu synthesis."""

    runner_type = RUNNER_TYPE

    def __init__(self, config: ProviderConfig, store: ArtifactStore, *, worker_path: Path | None = None):
        self.tts = VieNeuTtsRunner(store)
        self.audio = VieNeuAudioRunner(config, store, worker_path=worker_path)

    def execute(self, packet):
        step = (packet.task_config or {}).get("step")
        if step == "tts":
            return self.tts.execute(packet)
        if step == "audio":
            return self.audio.execute(packet)
        raise ValueError(f"VieNeu runner does not serve step {step!r}")

    def cancel(self, task_id):
        return self.audio.cancel(task_id)

    def health(self):
        return self.audio.health()

    def classify_error(self, error):
        return self.audio.classify_error(error)


class VieNeuProvider(RunnerProvider):
    name = RUNNER_TYPE
    roles = [Role.TTS_ADAPTER.value]
    max_concurrency = 1

    def __init__(self, config: ProviderConfig, store: ArtifactStore, *, worker_path: Path | None = None,
                 monotonic=time.monotonic):
        self.config = config
        self.store = store
        self.worker_path = Path(worker_path) if worker_path else WORKER_PATH
        self._monotonic = monotonic
        self._cache: tuple[float, tuple[bool, str | None, str]] | None = None

    # -- health --------------------------------------------------------------------------------

    def _check(self) -> tuple[bool, str | None, str]:
        """(ok, error_code, message). Cached ~60s. Never loads the model."""
        now = self._monotonic()
        if self._cache is not None and now - self._cache[0] < HEALTH_TTL:
            return self._cache[1]
        result = self._check_uncached()
        self._cache = (now, result)
        return result

    def _check_uncached(self) -> tuple[bool, str | None, str]:
        python = self.config.resolved_vieneu_python()
        if python is None or not Path(python).is_file():
            return False, "vieneu_missing", ("VieNeu-TTS not found: set STORYFLOW_VIENEU_ROOT to the checkout that "
                                             "has a .venv (or STORYFLOW_VIENEU_PYTHON)")
        try:
            done = subprocess.run([str(python), str(self.worker_path), "check"], stdin=subprocess.DEVNULL,
                                  capture_output=True, timeout=CHECK_TIMEOUT, env=clean_env(),
                                  cwd=tempfile.gettempdir())
        except subprocess.TimeoutExpired:
            return False, "check_timeout", "VieNeu import check timed out"
        except (OSError, subprocess.SubprocessError):
            return False, "vieneu_spawn_failed", "cannot start the VieNeu interpreter; check STORYFLOW_VIENEU_PYTHON"
        for raw in done.stdout.splitlines():
            event = parse_event_line(raw.strip())
            if event and event.get("ok") is True and event.get("vieneu_importable") is True:
                return True, None, ""
        return False, "vieneu_not_importable", ("the `vieneu` package is not importable in the configured "
                                                "interpreter; install VieNeu-TTS into that venv")

    def status(self) -> ProviderStatus:
        ok, code, message = self._check()
        if ok:
            return ProviderStatus(name=RUNNER_TYPE, kind="tts", state=READY, message="VieNeu-TTS is importable",
                                  details={"engine": ENGINE})
        return ProviderStatus(name=RUNNER_TYPE, kind="tts", state=UNAVAILABLE, message=message,
                              details={"reason": code})

    # -- RunnerProvider ------------------------------------------------------------------------

    def detect(self) -> list[DetectedRunner]:
        ok, code, message = self._check()
        health = RunnerHealth(ok=ok, runner_type=RUNNER_TYPE, state="ready" if ok else "offline",
                              error_code=None if ok else code, error_message=None if ok else message)
        return [DetectedRunner(RUNNER_ID, health)]

    def build(self, external_id: str = RUNNER_ID) -> AgentRunner:
        return VieNeuPipelineRunner(self.config, self.store, worker_path=self.worker_path)


__all__ = ["ENGINE", "VieNeuAudioRunner", "VieNeuPipelineRunner", "VieNeuProvider", "VieNeuTtsRunner",
           "pcm_wav_duration_ms", "scrub"]
