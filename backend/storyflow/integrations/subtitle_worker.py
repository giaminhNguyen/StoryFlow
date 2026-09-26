"""Standalone subtitle worker: ``python subtitle_worker.py <check|list|fetch>``.

Runs in its own interpreter (STORYFLOW_SUBTITLE_PYTHON) so StoryFlow never imports the upstream
``app`` package. Reads ONE JSON request on stdin, writes ONE JSON object on stdout, exits 0 whenever
it produced JSON (errors are encoded as ``{"ok": false, "error": ..., "message": ...}``); a nonzero
exit means a crash. Only stdlib is used here; it imports upstream ``app.services.subtitles`` and calls
its public functions (never the upstream DB).

Request: {"backend_dir": str, "video_id": str, "languages": [..], "preference": str, "allow_translation": bool}
Upstream import creates its data dir at import time; it is redirected to a temp dir via DATA_DIR /
DATABASE_URL before the import so nothing is written inside external/.
"""

import importlib
import json
import os
import re
import shutil
import sys
import tempfile

_PATH_RE = re.compile(r"(?:[A-Za-z]:)?[\\/](?:[^\s\"'<>|:]+[\\/])+[^\s\"'<>|:]*")


def _clean(text) -> str:
    text = _PATH_RE.sub("<path>", str(text)).replace("\n", " ").strip()
    return text[:300]


def _fail(error: str, message: str) -> dict:
    return {"ok": False, "error": error, "message": _clean(message)}


# Upstream exception classes are matched by NAME along the MRO (the worker never imports them eagerly).
# ``blocked`` is checked first because IpBlocked derives from RequestBlocked.
_BLOCKED_NAMES = frozenset({"RequestBlocked", "IpBlocked", "YouTubeRequestFailed", "PoTokenRequired",
                            "TooManyRequests"})
_LANGUAGE_NAMES = frozenset({"NotTranslatable", "TranslationLanguageNotAvailable"})
_VIDEO_NAMES = frozenset({"VideoUnavailable", "VideoUnplayable", "AgeRestricted", "InvalidVideoId",
                          "TranscriptsDisabled", "NoTranscriptFound", "NoTranscriptAvailable"})


def _upstream_error_kind(exc) -> str:
    """Error code for an exception raised by the upstream provider for ONE video.

    blocked / language_unavailable / video_unavailable for the known classes, ``subtitle_failed`` (a per-item,
    permanent error) for anything unexpected. Worker-level problems keep their own codes (import_error, internal).
    """
    names = {c.__name__ for c in type(exc).__mro__}
    if names & _BLOCKED_NAMES:
        return "blocked"
    if names & _LANGUAGE_NAMES:
        return "language_unavailable"
    if names & _VIDEO_NAMES:
        return "video_unavailable"
    return "subtitle_failed"


def _version(name: str):
    from importlib import metadata
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _import_upstream(backend_dir: str):
    if backend_dir and backend_dir not in sys.path:
        sys.path.insert(0, backend_dir)
    return importlib.import_module("app.services.subtitles")


def _track(t: dict) -> dict:
    return {"language": t["language"], "language_code": t["language_code"],
            "is_generated": bool(t["is_generated"]), "is_translatable": bool(t["is_translatable"])}


def handle(op: str, req: dict) -> dict:
    backend_dir = req.get("backend_dir") or ""
    if not backend_dir or not os.path.isdir(backend_dir):
        return _fail("import_error", "upstream backend dir not found")
    try:
        mod = _import_upstream(backend_dir)
    except ImportError as exc:
        return _fail("import_error", f"cannot import upstream provider (missing module: {getattr(exc, 'name', None) or 'unknown'})")
    if op == "check":
        return {"ok": True, "upstream_importable": True,
                "versions": {n: _version(n) for n in ("requests", "youtube-transcript-api", "pydantic-settings")}}
    if op not in ("list", "fetch"):
        return _fail("internal", f"unknown op {op}")
    video_id = req.get("video_id")
    if not isinstance(video_id, str) or not video_id:
        return _fail("internal", "video_id missing")
    try:
        if op == "list":
            return {"ok": True, "tracks": [_track(t) for t in mod.available_transcripts(video_id)]}
        transcript, translated, snippets = mod.fetch_selected(
            video_id, req.get("languages") or ["original"], req.get("preference") or "any",
            bool(req.get("allow_translation", True)))
        return {"ok": True, "subtitle": {
            "language": transcript.language, "language_code": transcript.language_code,
            "is_generated": bool(transcript.is_generated), "is_translatable": bool(transcript.is_translatable),
            "translated": bool(translated),
            "snippets": [{"text": s["text"], "start": float(s["start"]), "duration": float(s.get("duration", 0) or 0)}
                         for s in snippets]}}
    except mod.BlockedByYouTube as exc:
        return _fail("blocked", str(exc) or "blocked")
    except mod.LanguageUnavailable as exc:
        return _fail("language_unavailable", str(exc) or "language unavailable")
    except mod.SubtitleUnavailable as exc:
        return _fail("no_subtitle", str(exc) or "no subtitle")
    except OSError as exc:  # requests/urllib3 connection + timeout errors derive from OSError: transient
        return _fail("network", f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 - worker boundary: an odd video must not look like a broken install
        return _fail(_upstream_error_kind(exc), f"{type(exc).__name__}: {exc}")


def main(argv) -> int:
    real_out = sys.stdout
    if hasattr(real_out, "reconfigure"):
        real_out.reconfigure(encoding="utf-8")
    sys.stdout = sys.stderr  # stray prints must never corrupt the JSON channel
    op = argv[1] if len(argv) > 1 else ""
    tmp_owned = None
    if not os.environ.get("STORYFLOW_WORKER_TMP"):
        tmp_owned = tempfile.mkdtemp(prefix="sfsub_")
    tmp = os.environ.get("STORYFLOW_WORKER_TMP") or tmp_owned
    os.environ["DATA_DIR"] = os.path.join(tmp, "data")
    os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tmp, "unused.db").replace("\\", "/")
    try:
        try:
            req = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
            if not isinstance(req, dict):
                raise ValueError("request must be an object")
        except ValueError:
            result = _fail("internal", "bad request json")
        else:
            result = handle(op, req)
    finally:
        if tmp_owned:
            shutil.rmtree(tmp_owned, ignore_errors=True)
    real_out.write(json.dumps(result, ensure_ascii=False))
    real_out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
