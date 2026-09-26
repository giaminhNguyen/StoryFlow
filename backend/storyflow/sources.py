"""Source ingestion (roadmap 4.1): turn what a person types into a list of videos to process.

Accepted inputs (``parse_source``)::

    https://www.youtube.com/watch?v=ID   youtu.be/ID   /shorts/ID   /live/ID   /embed/ID   or a bare 11-char ID   -> video
    https://www.youtube.com/playlist?list=PL...                                                          -> playlist
    https://www.youtube.com/@handle   /channel/UC...   /c/name   /user/name   or a bare @handle       -> channel
    inbox:file.txt                                                                                        -> local file

Listing a channel / playlist goes through a ``VideoLister`` (default ``YtDlpLister``: an isolated
``python -m yt_dlp --flat-playlist`` subprocess, no API key). A channel's uploads tab is newest first; a
PLAYLIST comes back in playlist order (YouTube appends new items at the bottom), so "the first N" of a
playlist are not the newest. Nothing here touches the database. Local files are only ever read from the *inbox* directory (a bare file name, size-capped,
never a path) so an API caller cannot make StoryFlow read arbitrary files.

Error codes (``SourceError.code``): ``invalid_source``, ``lister_unavailable`` (yt-dlp not installed),
``lister_timeout`` (transient), ``lister_failed`` (channel not found / blocked / unreadable).
"""

from __future__ import annotations

import abc
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

VIDEO, PLAYLIST, CHANNEL, LOCAL = "video", "playlist", "channel", "local"
FEED_KINDS = (PLAYLIST, CHANNEL)

# ``\Z`` (not ``$``): ``$`` also matches before a trailing newline, so an id smuggled in as ``v=ID%0A`` would pass.
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}\Z")
_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,64}\Z")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{20,30}\Z")
_HANDLE_RE = re.compile(r"^@[\w.\-]{1,100}\Z")
_NAME_RE = re.compile(r"^[\w.\-]{1,100}\Z")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
INBOX_NAME_RE = re.compile(r"^[\w][\w .\-]{0,118}\.(txt|srt|vtt)\Z", re.UNICODE | re.IGNORECASE)
INBOX_SUFFIXES = (".txt", ".srt", ".vtt")
MAX_INBOX_BYTES = 5 * 1024 * 1024
MAX_SOURCE_LENGTH = 300


class SourceError(ValueError):
    """A source could not be understood / listed. ``code`` is stable; the message is safe to show."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedSource:
    kind: str        # video | playlist | channel | local
    ref: str         # video id | playlist id | canonical channel URL | inbox file name
    original: str = ""


@dataclass(frozen=True)
class VideoRef:
    video_id: str
    title: str | None = None
    duration_seconds: int | None = None


@dataclass
class ListedVideos:
    title: str | None
    videos: list[VideoRef] = field(default_factory=list)
    partial: bool = False        # the listing ended with errors: the list may be cut short (never silently complete)


def _only_dots(name: str) -> bool:
    return set(name) <= {"."}


def parse_source(text) -> ParsedSource:
    """Classify one user-supplied source string. Raises ``SourceError("invalid_source", ...)``."""
    if not isinstance(text, str) or not text.strip():
        raise SourceError("invalid_source", "source is empty")
    raw = text.strip()
    if len(raw) > MAX_SOURCE_LENGTH:
        raise SourceError("invalid_source", "source is too long")
    if raw.lower().startswith("inbox:"):
        name = raw[6:].strip()
        if not INBOX_NAME_RE.match(name) or ".." in name:
            raise SourceError("invalid_source", "inbox source must be a plain .txt/.srt/.vtt file name")
        return ParsedSource(LOCAL, name, raw)
    if VIDEO_ID_RE.match(raw):
        return ParsedSource(VIDEO, raw, raw)
    if _HANDLE_RE.match(raw) and not _only_dots(raw[1:]):
        return ParsedSource(CHANNEL, f"https://www.youtube.com/{raw}", raw)
    candidate = raw if "://" in raw else "https://" + raw
    try:
        url = urlparse(candidate)
    except ValueError:
        raise SourceError("invalid_source", "not a valid URL") from None
    host = (url.hostname or "").lower()
    parts = [unquote(p) for p in url.path.split("/") if p]   # browsers copy @%E6%97%A5 for @日
    query = parse_qs(url.query)
    if host == "youtu.be":
        if parts and VIDEO_ID_RE.match(parts[0]):
            return ParsedSource(VIDEO, parts[0], raw)
        raise SourceError("invalid_source", "not a YouTube video link")
    if host not in _YOUTUBE_HOSTS:
        raise SourceError("invalid_source", "only YouTube links are supported")
    if parts[:1] == ["watch"]:
        vid = (query.get("v") or [""])[0]
        if VIDEO_ID_RE.match(vid):
            return ParsedSource(VIDEO, vid, raw)   # a watch link with &list= is still that one video
        raise SourceError("invalid_source", "not a YouTube video link")
    if parts[:1] in (["shorts"], ["live"], ["embed"]) and len(parts) >= 2 and VIDEO_ID_RE.match(parts[1]):
        return ParsedSource(VIDEO, parts[1], raw)
    if parts[:1] == ["playlist"]:
        pid = (query.get("list") or [""])[0]
        if _PLAYLIST_ID_RE.match(pid):
            return ParsedSource(PLAYLIST, pid, raw)
        raise SourceError("invalid_source", "playlist link has no list id")
    if parts and parts[0].startswith("@") and _HANDLE_RE.match(parts[0]) and not _only_dots(parts[0][1:]):
        return ParsedSource(CHANNEL, f"https://www.youtube.com/{parts[0]}", raw)
    if len(parts) >= 2 and parts[0] == "channel" and _CHANNEL_ID_RE.match(parts[1]):
        return ParsedSource(CHANNEL, f"https://www.youtube.com/channel/{parts[1]}", raw)
    if len(parts) >= 2 and parts[0] in ("c", "user") and _NAME_RE.match(parts[1]) and not _only_dots(parts[1]):
        return ParsedSource(CHANNEL, f"https://www.youtube.com/{parts[0]}/{parts[1]}", raw)
    raise SourceError("invalid_source", "unsupported YouTube link (use a video, playlist or channel link)")


# ---------------------------------------------------------------------------- listers


class VideoLister(abc.ABC):
    @abc.abstractmethod
    def list_videos(self, source: ParsedSource, limit: int | None, timeout: float | None = None) -> ListedVideos:
        """Videos of a channel/playlist (at most ``limit`` when given; a channel newest first, a playlist in
        playlist order). ``timeout`` (seconds) caps THIS call below the lister's own default. Raises SourceError."""


LISTER_MAX_CONCURRENT = 2
_LISTER_SLOTS = threading.BoundedSemaphore(LISTER_MAX_CONCURRENT)   # process-wide: yt-dlp is heavy, YouTube throttles


class YtDlpLister(VideoLister):
    """``python -m yt_dlp --flat-playlist`` in a subprocess (own interpreter, hard timeout, no API key)."""

    def __init__(self, python: str | None = None, timeout: float = 120.0, runner=subprocess.run):
        self.python = python or sys.executable
        self.timeout = timeout
        self._run = runner

    @staticmethod
    def url_for(source: ParsedSource) -> str:
        if source.kind == CHANNEL:
            return source.ref.rstrip("/") + "/videos"          # the uploads tab: no Shorts / live tabs
        if source.kind == PLAYLIST:
            return f"https://www.youtube.com/playlist?list={source.ref}"
        raise SourceError("invalid_source", "only channels and playlists can be listed")

    # One JSON object per entry: a title can contain tabs / newlines but can never forge a second row.
    PRINT_TEMPLATE = "%(.{id,duration,playlist_title,title})j"

    def argv(self, source: ParsedSource, limit: int | None) -> list[str]:
        # --ignore-config: a user's yt-dlp.conf (--playlist-reverse, --match-filter, --proxy ...) must not change
        # what we list; --no-cache-dir: never write next to the user's files.
        cmd = [self.python, "-m", "yt_dlp", "--flat-playlist", "--no-warnings", "--ignore-errors",
               "--ignore-config", "--no-cache-dir", "--socket-timeout", "20", "--extractor-retries", "2"]
        if limit:
            cmd += ["--playlist-end", str(int(limit))]
        cmd += ["--print", self.PRINT_TEMPLATE, self.url_for(source)]
        return cmd

    def list_videos(self, source: ParsedSource, limit: int | None, timeout: float | None = None) -> ListedVideos:
        cmd = self.argv(source, limit)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
        budget = self.timeout if timeout is None else max(0.001, min(self.timeout, float(timeout)))
        started = time.monotonic()
        if not _LISTER_SLOTS.acquire(timeout=budget):
            raise SourceError("lister_timeout", "other channel listings are still running; try again later")
        try:
            done = self._run(cmd, capture_output=True, timeout=max(0.001, budget - (time.monotonic() - started)),
                             env=env, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            raise SourceError("lister_timeout", "listing the channel timed out; try again later") from None
        except OSError:
            raise SourceError("lister_unavailable", "cannot start the channel lister (check STORYFLOW_YTDLP_PYTHON)") \
                from None
        finally:
            _LISTER_SLOTS.release()
        stdout = done.stdout.decode("utf-8", "replace") if isinstance(done.stdout, bytes) else (done.stdout or "")
        stderr = done.stderr.decode("utf-8", "replace") if isinstance(done.stderr, bytes) else (done.stderr or "")
        if "No module named yt_dlp" in stderr:
            raise SourceError("lister_unavailable",
                              "yt-dlp is not installed: pip install -r backend/requirements-channel.txt")
        listed = parse_lister_output(stdout)
        failed = done.returncode != 0 or "ERROR" in stderr
        if not listed.videos:
            if failed:
                raise SourceError("lister_failed", "could not list videos (channel not found, private or blocked)")
        elif failed:
            listed.partial = True   # e.g. a 429 / network drop half way: what we have may be cut short
        return listed


def _json_row(line: str):
    """(id, duration, playlist_title, title) of one JSON line printed by yt-dlp, or None."""
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    text = lambda v: v if isinstance(v, str) else ("NA" if v is None else str(v))   # noqa: E731
    return (text(obj.get("id")).strip(), text(obj.get("duration")), text(obj.get("playlist_title")).strip(),
            text(obj.get("title")).strip())


def parse_lister_output(stdout: str) -> ListedVideos:
    """Parse the lister output: one JSON object per line (``{"id", "duration", "playlist_title", "title"}``) or the
    older ``id<TAB>duration<TAB>playlist_title<TAB>title`` lines; ignore private/deleted/malformed rows."""
    videos: list[VideoRef] = []
    seen: set[str] = set()
    title = None
    for line in stdout.split("\n"):   # not splitlines(): U+2028 / NEL inside a video title must not cut the row
        if line.lstrip().startswith("{"):
            row = _json_row(line)
            if row is None:
                continue
            video_id, duration, playlist_title, video_title = row
        else:
            cols = line.split("\t", 3)
            if len(cols) < 4:
                continue
            video_id, duration, playlist_title, video_title = (c.strip() for c in cols)
        if not VIDEO_ID_RE.match(video_id) or video_id in seen:
            continue
        if video_title.lower() in ("[private video]", "[deleted video]", "[unavailable video]"):
            continue
        seen.add(video_id)
        if title is None and playlist_title and playlist_title != "NA":
            title = playlist_title
        try:
            secs = int(float(duration))
        except (ValueError, OverflowError):   # "NA", "", "nan", "inf"
            secs = None
        if secs is not None and secs < 0:
            secs = None
        videos.append(VideoRef(video_id, None if video_title in ("", "NA") else video_title, secs))
    if title:
        title = re.sub(r" - (Videos|Video|Playlist)$", "", title)[:255]
    return ListedVideos(title, videos)


class FakeVideoLister(VideoLister):
    """Deterministic lister for tests: ``store`` maps a channel URL / playlist id -> (title, [VideoRef, ...])
    ordered newest first. Records every call in ``calls``."""

    def __init__(self, store: dict | None = None):
        self.store = store or {}
        self.calls: list[tuple[str, str, int | None]] = []
        self.timeouts: list[float | None] = []      # the per-call time budget the caller allowed

    def list_videos(self, source: ParsedSource, limit: int | None, timeout: float | None = None) -> ListedVideos:
        self.calls.append((source.kind, source.ref, limit))
        self.timeouts.append(timeout)
        entry = self.store.get(source.ref)
        if entry is None:
            raise SourceError("lister_failed", "could not list videos (channel not found, private or blocked)")
        if isinstance(entry, Exception):
            raise entry
        title, videos = entry
        return ListedVideos(title, list(videos[:limit] if limit else videos))


# ---------------------------------------------------------------------------- inbox (local files)


def _resolve_inbox_file(inbox_dir, name: str) -> Path | None:
    if inbox_dir is None or not isinstance(name, str) or not INBOX_NAME_RE.match(name) or ".." in name:
        return None
    root = Path(inbox_dir)
    try:
        path = (root / name).resolve()
        if root.resolve() not in path.parents or not path.is_file():
            return None
    except OSError:
        return None
    return path


def find_inbox_file(inbox_dir, video_id: str) -> str | None:
    """Name of ``<video_id>.txt|srt|vtt`` in the inbox, if the operator dropped one (explicit source)."""
    if inbox_dir is None or not isinstance(video_id, str) or not VIDEO_ID_RE.match(video_id):
        return None   # a config value of the wrong type (e.g. a number) must not crash the pipeline
    for suffix in INBOX_SUFFIXES:
        if _resolve_inbox_file(inbox_dir, video_id + suffix) is not None:
            return video_id + suffix
    return None


_TIMING_RE = re.compile(r"^\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}\s*-->\s*(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{1,3}")
# Only real markup: <i> </i> <c.color> <v Name> <font ...> and <00:00:01.500> karaoke stamps. A spoken
# "3 < 5 and 7 > 2" must survive, so a tag has to start with a letter, "/" + letter, or a timestamp.
_TAG_RE = re.compile(r"</?[A-Za-z][^<>]{0,79}>|<\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3}>")
_VTT_BLOCK_RE = re.compile(r"^(WEBVTT|NOTE|STYLE|REGION)(\s|\Z)")   # case-sensitive, as in the WebVTT spec


def _block_has_timing(lines: list[str], start: int) -> bool:
    for line in lines[start:]:
        if not line.strip():
            return False
        if _TIMING_RE.match(line):
            return True
    return False


def subtitle_text_to_plain(text: str) -> str:
    """Plain transcript from .txt / .srt / .vtt content: drops cue numbers/ids, timings, the WEBVTT header and
    NOTE / STYLE / REGION blocks, and markup tags; consecutive duplicate lines collapse to one.

    Metadata is recognised structurally (a block that starts with the keyword and holds no timing line), so a
    spoken line such as "Note that the door is open", "Style is everything" or a bare "2024" is kept."""
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    skipping = False        # inside a WEBVTT header / NOTE / STYLE / REGION block (until the next blank line)
    block_start = True
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            skipping, block_start = False, True
            continue
        if skipping:
            continue
        first, block_start = block_start, False
        if first and _VTT_BLOCK_RE.match(line) and not _block_has_timing(lines, i):
            skipping = True
            continue
        if line.startswith("X-TIMESTAMP") or _TIMING_RE.match(line):
            continue
        next_is_timing = i + 1 < len(lines) and bool(_TIMING_RE.match(lines[i + 1]))
        if next_is_timing and (first or line.isdigit()):   # SRT cue number / WebVTT cue identifier
            continue
        cleaned = _TAG_RE.sub("", line).strip()
        if cleaned and (not out or out[-1] != cleaned):
            out.append(cleaned)
    return "\n".join(out)


def decode_inbox_bytes(raw: bytes) -> str | None:
    """Text of an inbox file saved by any common Windows / Unicode editor, or None when it is not text.

    UTF-8 (with or without BOM) first; a UTF-16 BOM (Notepad's "Unicode") next; cp1252 last (old ANSI files).
    Binary junk (NUL bytes / many control characters) is refused whatever encoding would accept it."""
    text = None
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            text = raw.decode("utf-16")
        except UnicodeDecodeError:
            text = None
    if text is None:
        for encoding in ("utf-8-sig", "cp1252"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    if text is None or "\x00" in text:
        return None
    controls = sum(1 for ch in text if ch < " " and ch not in "\t\n\r\f")
    return None if controls > max(2, len(text) // 100) else text


def read_inbox_text(inbox_dir, name: str) -> str | None:
    """Plain text of an inbox file, or None when it does not exist / is unsafe / too big / not text."""
    path = _resolve_inbox_file(inbox_dir, name)
    if path is None:
        return None
    try:
        if path.stat().st_size > MAX_INBOX_BYTES:
            return None
        raw = decode_inbox_bytes(path.read_bytes())
    except OSError:
        return None
    if raw is None:
        return None
    if path.suffix.lower() in (".srt", ".vtt"):
        return subtitle_text_to_plain(raw)
    return raw.replace("\r\n", "\n").replace("\r", "\n").strip()   # Notepad / Windows editors save CRLF
