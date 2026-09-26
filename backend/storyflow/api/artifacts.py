"""Safe, read-only serving of stored generated artifacts (story text, source, canon, TTS, audio).

    GET|HEAD /api/artifacts/{rel_path:path}

Contract (stable):
  * The caller passes a store-RELATIVE path exactly as read models expose it.
  * EVERY refusal -- malformed/traversal path, missing file, directory, not under ``projects/``,
    hidden/temp name, disallowed extension, symlink escape -- is the SAME response:
    404 ``not_found`` "not found" with empty details. Nothing is echoed (no path, root, OS text),
    so attack attempts are indistinguishable from an ordinary miss and the layout cannot be probed.
  * The only distinct refusal is a file above the size cap: 422 ``validation`` "artifact too large".
    Text-like artifacts are capped at DEFAULT_MAX_BYTES = 256 MB; AUDIO files (.wav .mp3 .ogg .flac .m4a, e.g.
    the joined ``final.wav`` of a long story: ~345 MB per hour at 48 kHz / 16 bit) at DEFAULT_AUDIO_MAX_BYTES = 4 GiB.
    Both are configurable: ``app.state.audio_max_bytes`` / ``app.state.artifact_max_bytes`` (tests), else the env vars
    ``STORYFLOW_AUDIO_MAX_MB`` / ``STORYFLOW_ARTIFACT_MAX_MB`` (whole megabytes; invalid values are ignored). An
    explicit ``artifact_max_bytes`` on the app also applies to audio unless ``audio_max_bytes`` is set as well.
  * Textual checks run BEFORE any filesystem access; the filesystem is then only touched through
    ArtifactStore.resolve (Phase 3 root-escape protection) plus a regular-file check.
  * Content-Type comes from a fixed extension map (never sniffed); ``nosniff``, inline disposition
    with the bare filename, ``Cache-Control: no-cache``, ETag/Last-Modified and Range come from
    starlette FileResponse (streamed from disk, never read whole into memory).
"""

import os
import re
from pathlib import PurePosixPath

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from ..artifacts import PathTraversalError
from ..errors import NotFound, ValidationFailed

router = APIRouter()

DEFAULT_MAX_BYTES = 256 * 1024 * 1024
DEFAULT_AUDIO_MAX_BYTES = 4 * 1024 * 1024 * 1024
AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".ogg", ".flac", ".m4a"})
MAX_PATH_LENGTH = 512

CONTENT_TYPES = {
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".json": "application/json",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
}

# Conservative segment grammar: starts alphanumeric (so no ".", "..", ".tmp-*", "-x"), then
# letters/digits/._- only. This excludes ":", "\\", "%", NUL/control chars, spaces, "~" etc.
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _env_megabytes(name: str) -> int | None:
    """Whole megabytes from the environment as bytes; None when unset, not a positive integer or absurd."""
    raw = os.environ.get(name, "").strip()
    if not raw.isdigit() or not 0 < int(raw) <= 1_048_576:     # up to 1 TiB
        return None
    return int(raw) * 1024 * 1024


def size_cap(state, suffix: str) -> int:
    """Largest servable size for a file with this extension (see the module docstring for the precedence)."""
    text_cap = getattr(state, "artifact_max_bytes", None)
    if suffix.lower() in AUDIO_SUFFIXES:
        audio_cap = getattr(state, "audio_max_bytes", None)
        if audio_cap is not None:
            return audio_cap
        if text_cap is not None:                     # an explicit app-wide cap applies to audio too
            return text_cap
        return _env_megabytes("STORYFLOW_AUDIO_MAX_MB") or DEFAULT_AUDIO_MAX_BYTES
    if text_cap is not None:
        return text_cap
    return _env_megabytes("STORYFLOW_ARTIFACT_MAX_MB") or DEFAULT_MAX_BYTES


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":
        return True
    return any(tok.strip().removeprefix("W/") == etag for tok in header.split(","))


def _not_found() -> NotFound:
    return NotFound("not found")


def _valid_rel_path(rel_path: str) -> PurePosixPath | None:
    """Pure-text validation; returns the normalised path or None. Never touches the filesystem."""
    if not rel_path or len(rel_path) > MAX_PATH_LENGTH:
        return None
    parts = rel_path.split("/")  # backslashes are not in the grammar, so "\\" is rejected below
    if len(parts) < 2 or parts[0] != "projects":
        return None
    for seg in parts:
        if not _SEGMENT.match(seg) or seg.endswith(".") or len(seg) > 128:
            return None
        if seg.split(".", 1)[0].upper() in _RESERVED:
            return None
    if PurePosixPath(parts[-1]).suffix.lower() not in CONTENT_TYPES:
        return None
    return PurePosixPath(*parts)


@router.api_route("/artifacts/{rel_path:path}", methods=["GET", "HEAD"], include_in_schema=True)
def get_artifact(rel_path: str, request: Request):
    rel = _valid_rel_path(rel_path)
    if rel is None:
        raise _not_found()
    store = request.app.state.container.store
    try:
        target = store.resolve(str(rel))
    except PathTraversalError:
        raise _not_found() from None
    root = store.root.resolve()
    try:
        stat = target.stat()
    except OSError:
        raise _not_found() from None
    if not target.is_file() or not target.is_relative_to(root):
        raise _not_found()
    if target.suffix.lower() not in CONTENT_TYPES:  # symlink to a different extension
        raise _not_found()
    if stat.st_size > size_cap(request.app.state, target.suffix):
        raise ValidationFailed("artifact too large")
    etag = f'"{stat.st_size:x}-{stat.st_mtime_ns:x}"'
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache",
                                                  "X-Content-Type-Options": "nosniff"})
    return FileResponse(
        target,
        media_type=CONTENT_TYPES[rel.suffix.lower()],
        filename=rel.name,
        content_disposition_type="inline",
        stat_result=stat,
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache", "ETag": etag},
    )
