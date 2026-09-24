"""Safe, read-only serving of stored generated artifacts (story text, source, canon, TTS, audio).

    GET|HEAD /api/artifacts/{rel_path:path}

Contract (stable):
  * The caller passes a store-RELATIVE path exactly as read models expose it.
  * EVERY refusal -- malformed/traversal path, missing file, directory, not under ``projects/``,
    hidden/temp name, disallowed extension, symlink escape -- is the SAME response:
    404 ``not_found`` "not found" with empty details. Nothing is echoed (no path, root, OS text),
    so attack attempts are indistinguishable from an ordinary miss and the layout cannot be probed.
  * The only distinct refusal is a file above the size cap: 422 ``validation`` "artifact too large"
    (cap = ``app.state.artifact_max_bytes`` if set, else DEFAULT_MAX_BYTES = 256 MB).
  * Textual checks run BEFORE any filesystem access; the filesystem is then only touched through
    ArtifactStore.resolve (Phase 3 root-escape protection) plus a regular-file check.
  * Content-Type comes from a fixed extension map (never sniffed); ``nosniff``, inline disposition
    with the bare filename, ``Cache-Control: no-cache``, ETag/Last-Modified and Range come from
    starlette FileResponse (streamed from disk, never read whole into memory).
"""

import re
from pathlib import PurePosixPath

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response

from ..artifacts import PathTraversalError
from ..errors import NotFound, ValidationFailed

router = APIRouter()

DEFAULT_MAX_BYTES = 256 * 1024 * 1024
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
    cap = getattr(request.app.state, "artifact_max_bytes", DEFAULT_MAX_BYTES)
    if stat.st_size > cap:
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
