"""Real story runner over the local ``claude`` CLI (Phase 8, workstream B).

Invocation (verified against ``claude --help``, v2.1.x):

    claude -p --output-format json --no-session-persistence --tools "" \
           --append-system-prompt <one-line system prompt> [--model <m>]

* the prompt goes to STDIN (long sources exceed Windows argv limits);
* ``--tools ""`` disables every tool, so the model can only answer; there is no
  ``--dangerously-skip-permissions`` and no permission bypass of any kind;
* cwd is a fresh empty temp directory (no access to the repo), env is ``os.environ`` minus every
  ``STORYFLOW_*`` key. Credentials belong to the CLI's own login: StoryFlow never reads, stores or
  logs them and never probes authentication.

The runner only produces text: it reads input artifacts through the ArtifactStore, writes exactly one
output artifact through ``store.write`` and leaves business validation (schema / story length) to the
``OutputValidatingRunner`` wrapper. Prompts and model output are never logged, never put in metrics and
never put in a RunnerResult message (messages are fixed strings, scrubbed and bounded to 300 chars).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath

from ..agents import AgentRunner
from ..artifacts import ArtifactStore, PathTraversalError
from ..gateway import DetectedRunner
from ..protocol import ResultCode, RunnerHealth, RunnerResult, TaskPacket
from ..providers import READY, UNAVAILABLE, ProviderConfig, ProviderStatus
from ..roles import Role
from ..runtime.supervisor import RunnerProvider
from ..story_steps import CANON_SCHEMA, OutputPathError, safe_output_path

logger = logging.getLogger(__name__)

RUNNER_TYPE = "claude-cli"
EXTERNAL_ID = "claude-cli-1"
MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MESSAGE_LIMIT = 300
VERSION_TTL = 60.0
VERSION_TIMEOUT = 10.0
NOT_FOUND_MESSAGE = "claude CLI not found on PATH; set STORYFLOW_CLAUDE_CLI"

SYSTEM_PROMPT = (
    "You are a non-interactive text generator inside an automated pipeline. You have no tools and no "
    "files. Follow the user's format instructions exactly and output only the requested artifact, with "
    "no preface, commentary, questions or markdown fences around it."
)

# --- prompts (pure) --------------------------------------------------------------

_CANON_SCHEMA_TEXT = json.dumps(CANON_SCHEMA, ensure_ascii=False, indent=2)

_METHOD = (
    "Method (story-branch-writer, condensed): treat the reference as canon plus a storytelling model, not "
    "a plot to replay. Understand canon first: characters and their story functions, relationships, the "
    "chronological event chain with cause -> effect links, who knows what, the central conflict and "
    "emotional engine, setup/payoff patterns, and leverage points (facts which, if changed, force major "
    "downstream consequences)."
)


def build_canon_prompt(source_text: str) -> str:
    """Prompt asking for ONLY a JSON object matching CANON_SCHEMA."""
    return (
        f"{_METHOD}\n\n"
        "TASK: analyse the reference story below and output ONLY one JSON object (UTF-8, no code fence, "
        "no commentary) that follows this schema exactly. Field descriptions are given as values; "
        "'version' must be the integer 1; characters, events and leverage_points must be non-empty; "
        "every relationship 'from'/'to' must be an existing character id; ids are short unique strings; "
        "every string must be non-empty; 'causes' in an event is an optional list of earlier event ids.\n\n"
        f"{_CANON_SCHEMA_TEXT}\n\n"
        "Be structural, not a generic summary; keep it compact and factual.\n\n"
        "=== REFERENCE STORY ===\n"
        f"{source_text}\n"
        "=== END REFERENCE STORY ===\n"
    )


def build_story_prompt(canon_json: str, branch: str | None = None, direction: str | None = None,
                       target_length: int | str | None = None, source_text: str | None = None) -> str:
    """Prompt asking for ONLY the finished story as Markdown."""
    branch_line = (f"Branch (divergence premise, accept it): {branch}" if branch else
                   "Branch: none given. Identify 3-5 leverage points, weigh at most 3-5 candidates in a line "
                   "or two each, pick one with a clear conflict and room to escalate, and move on.")
    length_line = (f"Target length: AT LEAST {target_length} words (whitespace-separated words), i.e. as long as "
                   "or longer than the reference story. Do not summarise, compress or stop early: develop every "
                   "scene fully with dialogue and action until the length is reached and the story is finished."
                   if target_length else "Target length: a complete short story.")
    parts = [
        _METHOD,
        "TASK: write a genuinely new alternate-branch story. Define the divergence point and changed "
        "variables, then rebuild causality: never bolt the new premise onto the old plot; propagate "
        "consequences from character goals, knowledge and constraints; invalidate canon events that "
        "become impossible. Plan an arc (hook, early proof the branch matters, escalation, reveals, "
        "reversals, climax, ending) and write it scene by scene with action, dialogue, decisions and "
        "consequences rather than explanatory thought loops. Make the story work for a reader who has "
        "never seen the reference, open a compelling question early, pay it off, and end satisfyingly. "
        "Do not copy source wording. Do the planning silently.",
        branch_line,
        f"Direction: {direction}" if direction else "Direction: choose what fits the canon.",
        length_line,
        "OUTPUT: ONLY the finished story as Markdown text (a title heading is fine). No analysis, no "
        "outline, no notes, no preface, no code fence. Never mention file paths.",
        "=== CANON (JSON) ===\n" + canon_json + "\n=== END CANON ===",
    ]
    if source_text:
        parts.append("=== REFERENCE STORY ===\n" + source_text + "\n=== END REFERENCE STORY ===")
    return "\n\n".join(parts) + "\n"


# --- scrubbing / classification (pure) ----------------------------------------------

_SCRUBBERS = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{6,}"), "[redacted]"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "[redacted]"),
    (re.compile(r"[A-Za-z]:[\\/][^\s\"']*"), "[path]"),
    (re.compile(r"\\\\[^\s\"']+"), "[path]"),
    (re.compile(r"(?<![\w.])/(?:home|Users|tmp|var|etc|usr|mnt|root|opt|private)/[^\s\"']*"), "[path]"),
    (re.compile(r"[A-Za-z0-9+/_\-]{32,}={0,2}"), "[redacted]"),
)


def scrub_message(text, limit: int = MESSAGE_LIMIT) -> str:
    out = str(text or "")
    for pattern, repl in _SCRUBBERS:
        out = pattern.sub(repl, out)
    return out[:limit]


_AUTH = re.compile(r"not logged in|please run /?login|/login|invalid api key|authentication|unauthori[sz]ed|"
                   r"invalid x-api-key|oauth token|\b401\b", re.I)
_QUOTA = re.compile(r"credit balance|quota|billing|limit reached|reached (?:your|the) .{0,30}limit|"
                    r"resets? (?:at|in|on)|insufficient", re.I)
_RATE = re.compile(r"rate.?limit|\b429\b|usage limit|overloaded|\b529\b|too many requests", re.I)
_EPOCH = re.compile(r"\|\s*(\d{9,13})\b")
_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?)(?:Z|[+-]00:?00)?")
_RETRY = re.compile(r"retry[ _-]?after\D{0,3}(\d+(?:\.\d+)?)|in (\d+(?:\.\d+)?) ?(seconds?|secs?|minutes?|mins?)\b",
                    re.I)


def parse_retry_after(text: str) -> float | None:
    m = _RETRY.search(text or "")
    if not m:
        return None
    if m.group(1):
        return min(float(m.group(1)), 86400.0)
    value = float(m.group(2))
    if m.group(3).lower().startswith("min"):
        value *= 60
    return min(value, 86400.0)


def parse_quota_reset(text: str) -> datetime | None:
    """Naive UTC datetime from an epoch (``limit reached|1700000000``) or ISO hint, else None."""
    m = _EPOCH.search(text or "")
    if m:
        value = int(m.group(1))
        value = value / 1000 if value > 10**11 else value
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None
    m = _ISO.search(text or "")
    if m:
        try:
            return datetime.fromisoformat(m.group(1).replace(" ", "T"))
        except ValueError:
            return None
    return None


def classify_failure(text: str) -> RunnerResult | None:
    """Map CLI error text to a failure RunnerResult, or None when nothing specific matched."""
    if _AUTH.search(text) and not _QUOTA.search(text):
        return RunnerResult(ResultCode.AUTH_ERROR, error_code="auth_error",
                            error_message="claude CLI is not authenticated; log in with the claude CLI")
    if _QUOTA.search(text) and not re.search(r"rate.?limit|\b429\b|overloaded", text, re.I):
        return RunnerResult(ResultCode.QUOTA_EXHAUSTED, error_code="quota_exhausted",
                            quota_reset_at=parse_quota_reset(text),
                            error_message="claude CLI usage quota exhausted")
    if _RATE.search(text) or _QUOTA.search(text):
        return RunnerResult(ResultCode.RATE_LIMITED, error_code="rate_limited",
                            retry_after=parse_retry_after(text),
                            error_message="claude CLI rate limited or overloaded")
    return None


# --- output extraction (pure) ---------------------------------------------------------

_FENCE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*\n(.*?)\n?```\s*$", re.S)


def strip_fences(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1) if m else text


def extract_json_object(text: str) -> str | None:
    """The JSON object in ``text`` (fenced or surrounded by prose), or None."""
    text = strip_fences(text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start:end + 1]


# --- process helpers ---------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"


def clean_env(env=None) -> dict:
    return {k: v for k, v in (os.environ if env is None else env).items() if not k.upper().startswith("STORYFLOW_")}


def kill_process_tree(proc) -> None:
    pid = getattr(proc, "pid", None)
    try:
        if _IS_WINDOWS and pid:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=15,
                           stdin=subprocess.DEVNULL)
        elif pid:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


class _Capture(threading.Thread):
    """Reads a pipe up to ``cap`` bytes (kept), keeps draining beyond it (discarded)."""

    def __init__(self, stream, cap: int, on_overflow=None):
        super().__init__(daemon=True)
        self.stream, self.cap, self.on_overflow = stream, cap, on_overflow
        self.data = bytearray()
        self.overflow = False

    def run(self):
        try:
            while True:
                chunk = self.stream.read(65536)
                if not chunk:
                    return
                room = self.cap - len(self.data)
                if len(chunk) > room:
                    self.overflow = True
                    self.data += chunk[:max(room, 0)]
                    if self.on_overflow:
                        self.on_overflow()
                else:
                    self.data += chunk
        except (OSError, ValueError):
            return


def _feed(stream, data: bytes) -> None:
    try:
        stream.write(data)
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def _read_input(store: ArtifactStore, rel, project_id) -> str:
    """Read an input artifact through the store with the same rules as safe_output_path."""
    if not isinstance(rel, str) or not rel:
        raise OutputPathError("input path missing")
    try:
        store.resolve(rel)
    except PathTraversalError as exc:
        raise OutputPathError("unsafe input path") from exc
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    if len(parts) < 3 or parts[0] != "projects" or parts[1] != project_id:
        raise OutputPathError("input path must be under projects/<project_id>/")
    return store.read(rel).decode("utf-8")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ClaudeCliRunner(AgentRunner):
    runner_type = RUNNER_TYPE

    def __init__(self, config: ProviderConfig, store: ArtifactStore, *, popen=subprocess.Popen,
                 which=shutil.which):
        self.config = config
        self.store = store
        self._popen = popen
        self._which = which
        self._lock = threading.Lock()
        self._procs: dict[str, object] = {}
        self._cancelled: set[str] = set()

    # -- AgentRunner ---------------------------------------------------------------------

    def health(self) -> RunnerHealth:
        ok = self._resolve_cli() is not None
        return RunnerHealth(ok=ok, runner_type=self.runner_type, state="ready" if ok else "offline",
                            error_code=None if ok else "cli_not_found",
                            error_message=None if ok else NOT_FOUND_MESSAGE)

    def cancel(self, task_id: str) -> bool:
        with self._lock:
            proc = self._procs.get(task_id)
            if proc is None:
                return False
            self._cancelled.add(task_id)
        kill_process_tree(proc)
        return True

    def execute(self, packet: TaskPacket) -> RunnerResult:
        step = (packet.task_config or {}).get("step")
        if step not in ("canon", "story"):
            return _fail(ResultCode.TASK_FAILED, "unsupported_step", "unsupported step")
        try:
            rel = safe_output_path(packet, self.store, "canon.json" if step == "canon" else "story.md")
            prompt = self._build_prompt(packet, step)
        except OutputPathError:
            return _fail(ResultCode.TASK_FAILED, "bad_path", "packet path rejected")
        except FileNotFoundError:
            return _fail(ResultCode.TASK_FAILED, "missing_input", "input artifact not found")
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError):
            return _fail(ResultCode.TASK_FAILED, "bad_input", "input artifact unreadable")

        cli = self._resolve_cli()
        if cli is None:
            return _fail(ResultCode.RUNNER_CRASHED, "cli_not_found", NOT_FOUND_MESSAGE)
        return self._run(packet, step, rel, cli, prompt)

    def classify_error(self, error: BaseException) -> ResultCode:
        if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)):
            return ResultCode.TIMEOUT
        return ResultCode.RUNNER_CRASHED

    # -- internals -----------------------------------------------------------------------

    def _resolve_cli(self) -> str | None:
        return self._which(self.config.claude_cli or "claude")

    def _build_prompt(self, packet: TaskPacket, step: str) -> str:
        inputs = packet.inputs or {}
        pid = inputs.get("project_id")
        if not pid:
            raise OutputPathError("project_id missing")
        if step == "canon":
            return build_canon_prompt(_read_input(self.store, inputs.get("source_artifact"), pid))
        canon = _read_input(self.store, inputs.get("canon_artifact"), pid)
        json.loads(canon)
        source = None
        if inputs.get("source_artifact"):
            source = _read_input(self.store, inputs["source_artifact"], pid)
        return build_story_prompt(canon, inputs.get("branch"), inputs.get("direction"),
                                  inputs.get("target_length"), source)

    def _argv(self, cli: str) -> list[str]:
        argv = [cli, "-p", "--output-format", "json", "--no-session-persistence", "--tools", "",
                "--append-system-prompt", SYSTEM_PROMPT]
        if self.config.claude_model:
            argv += ["--model", self.config.claude_model]
        return argv

    def _run(self, packet: TaskPacket, step: str, rel: str, cli: str, prompt: str) -> RunnerResult:
        task_id = packet.task_id or packet.job_id or f"anon-{id(packet)}"
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="storyflow-claude-") as cwd:
            kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                      "cwd": cwd, "env": clean_env()}
            if _IS_WINDOWS:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            else:
                kwargs["start_new_session"] = True
            try:
                proc = self._popen(self._argv(cli), **kwargs)
            except FileNotFoundError:
                return _fail(ResultCode.RUNNER_CRASHED, "cli_not_found", NOT_FOUND_MESSAGE)
            except OSError:
                return _fail(ResultCode.RUNNER_CRASHED, "spawn_failed", "could not start claude CLI")
            with self._lock:
                self._procs[task_id] = proc
            out = _Capture(proc.stdout, MAX_STDOUT_BYTES, lambda: kill_process_tree(proc))
            err = _Capture(proc.stderr, MAX_STDERR_BYTES)
            feeder = threading.Thread(target=_feed, args=(proc.stdin, prompt.encode("utf-8")), daemon=True)
            for t in (out, err, feeder):
                t.start()
            timed_out = False
            try:
                proc.wait(timeout=self.config.story_timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_process_tree(proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    logger.warning("claude CLI process did not exit after kill")
            finally:
                with self._lock:
                    self._procs.pop(task_id, None)
                    cancelled = task_id in self._cancelled
                    self._cancelled.discard(task_id)
            for t in (out, err, feeder):
                t.join(timeout=5)
        duration_ms = int((time.monotonic() - started) * 1000)
        if cancelled:
            return _fail(ResultCode.CANCELLED, "cancelled", "task cancelled")
        if timed_out:
            return _fail(ResultCode.TIMEOUT, "timeout", "claude CLI timed out")
        if out.overflow:
            return _fail(ResultCode.INVALID_OUTPUT, "output_too_large", "claude CLI output too large")
        return self._finish(step, rel, proc.returncode, bytes(out.data), bytes(err.data), duration_ms)

    def _finish(self, step: str, rel: str, code: int, stdout: bytes, stderr: bytes, duration_ms: int) -> RunnerResult:
        try:
            out_text = stdout.decode("utf-8")
            err_text = stderr.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return _fail(ResultCode.INVALID_OUTPUT, "bad_encoding", "claude CLI output is not UTF-8")
        envelope = None
        try:
            parsed = json.loads(out_text) if out_text.strip() else None
            envelope = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            envelope = None
        result_text = envelope.get("result") if envelope else None
        result_text = result_text if isinstance(result_text, str) else ""
        failed = code != 0 or bool(envelope and (envelope.get("is_error") or
                                                  str(envelope.get("subtype", "")).startswith("error")))
        if failed:
            hint = f"{result_text}\n{err_text}"
            if envelope is None:
                hint += "\n" + out_text[:2000]
            classified = classify_failure(hint)
            if classified is not None:
                return classified
            if code != 0:
                return _fail(ResultCode.TRANSIENT_FAILURE, "cli_failed", f"claude CLI exited with status {code}")
            return _fail(ResultCode.TRANSIENT_FAILURE, "cli_error", "claude CLI reported an error")
        if envelope is None:
            return _fail(ResultCode.INVALID_OUTPUT, "invalid_envelope", "claude CLI output is not a JSON result")
        if not result_text.strip():
            return _fail(ResultCode.INVALID_OUTPUT, "empty_result", "claude CLI returned no text")
        if step == "canon":
            payload = extract_json_object(result_text)
            if payload is None:
                return _fail(ResultCode.INVALID_OUTPUT, "no_json", "claude CLI returned no JSON object")
        else:
            payload = strip_fences(result_text).strip() + "\n"
        self.store.write(rel, payload.encode("utf-8"))
        return RunnerResult(ResultCode.SUCCESS, artifacts={"outputs": [rel]}, metrics=_metrics(envelope, duration_ms))


def _metrics(envelope: dict, duration_ms: int) -> dict:
    metrics: dict = {"duration_ms": duration_ms}

    def number(value):
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    for key in ("total_cost_usd", "num_turns", "duration_api_ms"):
        if number(envelope.get(key)) is not None:
            metrics[key] = envelope[key]
    usage = envelope.get("usage")
    if isinstance(usage, dict):
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
            if number(usage.get(key)) is not None:
                metrics[key] = usage[key]
    return metrics


def _fail(code: ResultCode, error_code: str, message: str) -> RunnerResult:
    return RunnerResult(code, error_code=error_code, error_message=scrub_message(message))


# --- provider ---------------------------------------------------------------------------------


class ClaudeCliProvider(RunnerProvider):
    name = RUNNER_TYPE
    roles = [Role.STORY_WRITER.value]

    def __init__(self, config: ProviderConfig, store: ArtifactStore, *, max_concurrency: int = 1,
                 popen=subprocess.Popen, which=shutil.which, run=subprocess.run,
                 monotonic=time.monotonic):
        self.config, self.store = config, store
        self.max_concurrency = max_concurrency
        self._popen, self._which, self._run, self._monotonic = popen, which, run, monotonic
        self._cache: tuple[float, str | None] | None = None
        self._lock = threading.Lock()

    def _version(self) -> str | None:
        """``claude --version`` output (cached ~60s); None when missing/failing. No model call."""
        with self._lock:
            now = self._monotonic()
            if self._cache is not None and now - self._cache[0] < VERSION_TTL:
                return self._cache[1]
        cli = self._which(self.config.claude_cli or "claude")
        version = None
        if cli:
            try:
                proc = self._run([cli, "--version"], capture_output=True, timeout=VERSION_TIMEOUT,
                                 stdin=subprocess.DEVNULL, env=clean_env(), text=True, encoding="utf-8",
                                 errors="replace")
                if proc.returncode == 0:
                    version = scrub_message((proc.stdout or "").strip().splitlines()[0]
                                            if (proc.stdout or "").strip() else "unknown", 60)
            except (OSError, subprocess.SubprocessError):
                version = None
        with self._lock:
            self._cache = (self._monotonic(), version)
        return version

    def detect(self) -> list[DetectedRunner]:
        ok = self._version() is not None
        return [DetectedRunner(EXTERNAL_ID, RunnerHealth(
            ok=ok, runner_type=self.name, state="ready" if ok else "offline",
            error_code=None if ok else "cli_not_found", error_message=None if ok else NOT_FOUND_MESSAGE))]

    def build(self, external_id: str) -> AgentRunner:
        return ClaudeCliRunner(self.config, self.store, popen=self._popen, which=self._which)

    def status(self) -> ProviderStatus:
        version = self._version()
        if version is None:
            return ProviderStatus(RUNNER_TYPE, "story", UNAVAILABLE, NOT_FOUND_MESSAGE)
        return ProviderStatus(RUNNER_TYPE, "story", READY, "claude CLI available", {"version": version})
