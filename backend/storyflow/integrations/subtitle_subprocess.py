"""SubtitleClient that talks to the upstream provider through a worker SUBPROCESS.

The StoryFlow process never imports upstream code; ``subtitle_worker.py`` runs in the interpreter
``config.subtitle_python`` with a hard timeout. Messages raised here are generic and path-free (they
may reach the DB/API); details go to logger.debug only, and stderr is never stored.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from ..providers import READY, UNAVAILABLE, ProviderConfig, ProviderStatus
from ..subtitles import (
    BlockedByProvider,
    FetchedSubtitle,
    LanguageUnavailable,
    ProviderTimeout,
    ProviderUnavailable,
    SubtitleClient,
    SubtitleFetchFailed,
    SubtitleSnippet,
    SubtitlesUnavailable,
    SubtitleTrack,
)

logger = logging.getLogger("storyflow.subtitle")

WORKER_PATH = Path(__file__).with_name("subtitle_worker.py")
CHECK_TIMEOUT = 20.0
STATUS_CACHE_SECONDS = 30.0
_STDERR_LOG_LIMIT = 2000
_INSTALL_HINT = "subtitle provider dependencies missing: pip install -r backend/requirements-subtitle.txt"


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(proc.pid, 9)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_killing_tree(cmd, *, input, timeout, env=None, cwd=None):
    """subprocess.run-like: on timeout kills the child and its process tree, then raises TimeoutExpired."""
    kwargs = {"start_new_session": True} if os.name != "nt" else {}
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, cwd=cwd, **kwargs)
    try:
        out, err = proc.communicate(input.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


class SubprocessSubtitleClient(SubtitleClient):
    def __init__(self, config: ProviderConfig, *, runner=run_killing_tree, clock=time.monotonic):
        self._config = config
        self._runner = runner
        self._clock = clock
        self._status: ProviderStatus | None = None
        self._status_at = 0.0

    # --- worker plumbing ---
    def _call(self, op: str, request: dict, timeout: float) -> dict:
        request = dict(request, backend_dir=str(self._config.resolved_subtitle_backend_dir()))
        cmd = [self._config.subtitle_python, str(WORKER_PATH), op]
        with tempfile.TemporaryDirectory(prefix="sfsub_", ignore_cleanup_errors=True) as tmp:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
                       PYTHONIOENCODING="utf-8", STORYFLOW_WORKER_TMP=tmp)
            try:
                done = self._runner(cmd, input=json.dumps(request), timeout=timeout, env=env, cwd=tmp)
            except subprocess.TimeoutExpired as exc:
                raise ProviderTimeout("subtitle provider timed out") from exc
            except OSError as exc:
                logger.debug("subtitle worker cannot start: %s", exc)
                raise ProviderUnavailable(
                    "subtitle worker interpreter not available (check STORYFLOW_SUBTITLE_PYTHON)") from exc
        stderr = done.stderr
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", "replace")
        if stderr:
            logger.debug("subtitle worker stderr: %s", stderr[:_STDERR_LOG_LIMIT])
        stdout = done.stdout
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", "replace")
        try:
            payload = json.loads(stdout) if done.returncode == 0 and stdout else None
        except ValueError:
            payload = None
        if not isinstance(payload, dict) or "ok" not in payload:
            raise ProviderUnavailable("subtitle worker failed")
        if not payload["ok"]:
            self._raise(payload)
        return payload

    @staticmethod
    def _raise(payload: dict):
        error, message = payload.get("error"), str(payload.get("message", ""))[:300]
        logger.debug("subtitle worker error %s: %s", error, message)
        if error == "blocked":
            raise BlockedByProvider("subtitle provider blocked the request")
        if error == "no_subtitle":
            raise SubtitlesUnavailable("no subtitle for this video")
        if error == "video_unavailable":  # private / deleted / age-restricted / not playable: nothing to fetch
            raise SubtitlesUnavailable("video is unavailable (private, removed, age-restricted or not playable)")
        if error == "subtitle_failed":  # unexpected upstream failure for this one video: permanent for the item
            raise SubtitleFetchFailed("subtitle provider failed for this video")
        if error == "language_unavailable":
            raise LanguageUnavailable("requested subtitle language unavailable")
        if error == "network":  # transient connectivity problem upstream: retry later, not an operator fix
            raise ProviderTimeout("subtitle provider network error")
        if error == "import_error":
            raise ProviderUnavailable(_INSTALL_HINT)
        raise ProviderUnavailable("subtitle worker failed")

    # --- SubtitleClient ---
    def list_tracks(self, video_id: str) -> list[SubtitleTrack]:
        payload = self._call("list", {"video_id": video_id}, self._config.subtitle_timeout)
        try:
            return [SubtitleTrack(t["language"], t["language_code"], bool(t["is_generated"]),
                                  bool(t["is_translatable"])) for t in payload["tracks"]]
        except (KeyError, TypeError) as exc:
            raise ProviderUnavailable("subtitle worker failed") from exc

    def fetch(self, video_id: str, languages=None, preference="any", allow_translation=True) -> FetchedSubtitle:
        payload = self._call("fetch", {"video_id": video_id, "languages": languages or ["original"],
                                       "preference": preference, "allow_translation": allow_translation},
                             self._config.subtitle_timeout)
        try:
            s = payload["subtitle"]
            return FetchedSubtitle(
                language=s["language"], language_code=s["language_code"],
                is_generated=bool(s["is_generated"]), is_translatable=bool(s["is_translatable"]),
                translated=bool(s["translated"]),
                snippets=[SubtitleSnippet(x["text"], float(x["start"]), float(x.get("duration", 0.0)))
                          for x in s["snippets"]])
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderUnavailable("subtitle worker failed") from exc

    # --- readiness ---
    def status(self) -> ProviderStatus:
        now = self._clock()
        if self._status is not None and now - self._status_at < STATUS_CACHE_SECONDS:
            return self._status
        try:
            payload = self._call("check", {}, CHECK_TIMEOUT)
        except ProviderUnavailable as exc:
            status = ProviderStatus("external", "subtitle", UNAVAILABLE, str(exc))
        except ProviderTimeout:
            status = ProviderStatus("external", "subtitle", UNAVAILABLE, "subtitle worker check timed out")
        except (BlockedByProvider, SubtitlesUnavailable, LanguageUnavailable):
            status = ProviderStatus("external", "subtitle", UNAVAILABLE, "subtitle worker check failed")
        else:
            versions = {k: v for k, v in (payload.get("versions") or {}).items() if v}
            status = ProviderStatus("external", "subtitle", READY, "", {"versions": versions})
        self._status, self._status_at = status, now
        return status


__all__ = ["SubprocessSubtitleClient", "WORKER_PATH", "run_killing_tree"]
