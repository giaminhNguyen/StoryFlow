"""Subtitle source abstraction (Phase 3).

Maps 1:1 onto the public services API of the external subtitle_suppervip repo
(available_transcripts / fetch_selected / choose_transcript). StoryFlow code talks
only to SubtitleClient, so the real provider and the deterministic fake are
interchangeable in tests and in later runner phases.

The real adapter (ExternalSubtitleClient) loads the external ``app`` package lazily
on first use: that package needs its own dependency set (requests, youtube-transcript-api,
pydantic, ...) which is not in StoryFlow's venv, so importing this module must never
touch external code.

Capability gaps (Phase 3 audit of the upstream provider; Phase 4 work will need them):
1. Upstream REST exposes only track metadata + job-based file download; there is no
   HTTP endpoint that returns transcript text for a video_id. StoryFlow integrates
   in-process via ``fetch_selected``, never through the REST layer.
2. No "exact track for language code X" call: ``fetch_selected`` picks one track by
   its preference order. Select a specific track via the upstream ``choose_transcript``
   directly if ever needed.
3. No API to read back content of an already-stored subtitle file (only ``file_path``);
   re-reading requires the upstream ``storage.resolve_subtitle_path`` + file I/O.
4. Upstream stores subtitle preferences per-channel only; per-video overrides cannot be
   persisted in its DB model. Overrides must be passed as arguments per call (supported).
5. Policy/language defaults here mirror upstream ("original", "any", translation on).
   The real client additionally needs the upstream backend deps installed (see
   ``import_external_subtitles``'s error message).
"""

import abc
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class SubtitleTrack:
    language: str
    language_code: str
    is_generated: bool
    is_translatable: bool


@dataclass(frozen=True)
class SubtitleSnippet:
    text: str
    start: float
    duration: float


@dataclass(frozen=True)
class FetchedSubtitle:
    language: str
    language_code: str
    is_generated: bool
    is_translatable: bool
    translated: bool
    snippets: list


class SubtitlesUnavailable(Exception):
    """The video has no usable subtitle tracks (private/removed/disabled)."""


class LanguageUnavailable(Exception):
    """Subtitles exist but none match the requested languages (and no translation allowed/possible)."""


class BlockedByProvider(Exception):
    """The provider refused the request (IP blocked / rate limited / 429)."""


class SubtitleFetchFailed(Exception):
    """An unexpected upstream failure for THIS video (not a block, not a missing subtitle, and not an operator
    problem such as a missing install). Permanent for the item; it must never look like an operator fault, or one
    odd video (members-only, upcoming, ...) would pause a whole batch."""


class ProviderUnavailable(Exception):
    """Provider not installed/misconfigured (worker python, deps or upstream dir missing).
    Permanent until an operator fixes the configuration."""


class ProviderTimeout(Exception):
    """The provider call hit the hard timeout (transient)."""


def plain_text(subtitle: FetchedSubtitle) -> str:
    """Transcript rendered as plain text lines, mirroring the provider's txt serialization."""
    return "\n".join(snippet.text for snippet in subtitle.snippets)


class SubtitleClient(abc.ABC):
    """Contract StoryFlow uses to reach any subtitle source."""

    @abc.abstractmethod
    def list_tracks(self, video_id: str) -> list[SubtitleTrack]:
        """Caption tracks for a video. Raises SubtitlesUnavailable / BlockedByProvider."""

    @abc.abstractmethod
    def fetch(self, video_id: str, languages: list[str] | None = None,
              preference: str = "any", allow_translation: bool = True) -> FetchedSubtitle:
        """One selected transcript + its snippets. Raises LanguageUnavailable too."""


# --- deterministic fake ----------------------------------------------------


class FakeSubtitleClient(SubtitleClient):
    """Scripted, deterministic client for tests.

    ``store`` maps video_id -> {"tracks": [ {language, language_code, is_generated,
    is_translatable, snippets: [{text, start, duration}]} ], "error": None | "no_subtitle" | "blocked"}.
    Selection mirrors the provider's choose_transcript rules: language match first,
    then machine translation when allow_translation, else LanguageUnavailable.
    """

    def __init__(self, store: dict | None = None):
        self.store = store or {}

    def _entry(self, video_id: str):
        entry = self.store.get(video_id)
        if entry is None or entry.get("error") == "no_subtitle":
            raise SubtitlesUnavailable(f"no subtitle for {video_id}")
        if entry.get("error") == "blocked":
            raise BlockedByProvider(f"provider blocked {video_id}")
        return entry

    def list_tracks(self, video_id: str) -> list[SubtitleTrack]:
        entry = self._entry(video_id)
        return [
            SubtitleTrack(t["language"], t["language_code"], t["is_generated"], t["is_translatable"])
            for t in entry["tracks"]
        ]

    def fetch(self, video_id: str, languages=None, preference="any", allow_translation=True) -> FetchedSubtitle:
        entry = self._entry(video_id)
        ordered = (
            [t for t in entry["tracks"] if not t["is_generated"]]
            if preference == "manual"
            else [t for t in entry["tracks"] if t["is_generated"]]
            if preference == "auto"
            else entry["tracks"]
        )
        if not ordered:
            ordered = entry["tracks"] if preference == "any" else []
        if not ordered:
            raise LanguageUnavailable("no subtitle of the chosen type")
        for language in languages or ["original"]:
            if language == "original":
                return self._result(ordered[0], translated=False)
            for track in ordered:
                if track["language_code"].lower().split("-")[0] == language.lower().split("-")[0]:
                    return self._result(track, translated=False)
        if allow_translation:
            for language in languages or []:
                if language != "original":
                    for track in ordered:
                        if track["is_translatable"]:
                            return self._result(track, translated=True, to=language)
        raise LanguageUnavailable("no requested language available")

    def _result(self, track: dict, *, translated: bool, to: str | None = None) -> FetchedSubtitle:
        return FetchedSubtitle(
            language=track["language"],
            language_code=to or track["language_code"],
            is_generated=track["is_generated"],
            is_translatable=track["is_translatable"],
            translated=translated,
            snippets=[SubtitleSnippet(s["text"], s["start"], s.get("duration", 0.0)) for s in track["snippets"]],
        )


# --- real provider adapter --------------------------------------------------


def default_external_backend_dir() -> Path:
    return PROJECT_ROOT / "external" / "subtitle_suppervip" / "backend"


def import_external_subtitles(backend_dir: Path):
    """Import the external ``app.services.subtitles`` module. Raises ImportError with
    actionable text when the package or its dependencies are unavailable."""
    backend_dir = Path(backend_dir).resolve()
    if str(backend_dir) not in sys.path:
        sys.path.insert(0, str(backend_dir))
    try:
        return importlib.import_module("app.services.subtitles")
    except ImportError as exc:
        raise ImportError(
            "external subtitle provider not importable; need "
            f"'{backend_dir}' on sys.path with its backend deps installed "
            f"(pip install -r {backend_dir / 'requirements.txt'}): {exc}"
        ) from exc


# Upstream (youtube-transcript-api) exception classes are matched by NAME along the MRO, so this module never
# needs to import them. ``blocked`` wins over the others (IpBlocked derives from RequestBlocked).
UPSTREAM_BLOCKED_NAMES = frozenset({"RequestBlocked", "IpBlocked", "YouTubeRequestFailed", "PoTokenRequired",
                                    "TooManyRequests"})
UPSTREAM_LANGUAGE_NAMES = frozenset({"NotTranslatable", "TranslationLanguageNotAvailable"})
UPSTREAM_VIDEO_NAMES = frozenset({"VideoUnavailable", "VideoUnplayable", "AgeRestricted", "InvalidVideoId",
                                  "TranscriptsDisabled", "NoTranscriptFound", "NoTranscriptAvailable"})


def upstream_error_kind(exc: BaseException) -> str | None:
    """"blocked" | "language_unavailable" | "video_unavailable" for a known upstream exception, else None."""
    names = {c.__name__ for c in type(exc).__mro__}
    if names & UPSTREAM_BLOCKED_NAMES:
        return "blocked"
    if names & UPSTREAM_LANGUAGE_NAMES:
        return "language_unavailable"
    if names & UPSTREAM_VIDEO_NAMES:
        return "video_unavailable"
    return None


def _raise_unexpected(exc: Exception):
    """Map an exception the adapter did not expect to the per-item StoryFlow error (never a bare crash)."""
    kind = upstream_error_kind(exc)
    text = f"{type(exc).__name__}"
    if kind == "blocked":
        raise BlockedByProvider(text) from exc
    if kind == "language_unavailable":
        raise LanguageUnavailable(text) from exc
    if kind == "video_unavailable":
        raise SubtitlesUnavailable(text) from exc
    if isinstance(exc, OSError):  # connection / timeout errors are transient
        raise ProviderTimeout(text) from exc
    raise SubtitleFetchFailed(text) from exc


class ExternalSubtitleClient(SubtitleClient):
    """Thin adapter over the external subtitle_suppervip services. The provider module
    is imported lazily so constructing this client costs nothing in tests that only
    use the fake."""

    def __init__(self, backend_dir: Path | None = None, provider_builder=import_external_subtitles):
        self._backend_dir = Path(backend_dir) if backend_dir else default_external_backend_dir()
        self._builder = provider_builder
        self._mod = None

    def _module(self):
        if self._mod is None:
            self._mod = self._builder(self._backend_dir)
        return self._mod

    def list_tracks(self, video_id: str) -> list[SubtitleTrack]:
        mod = self._module()
        try:
            tracks = mod.available_transcripts(video_id)
        except mod.BlockedByYouTube as exc:
            raise BlockedByProvider(str(exc)) from exc
        except mod.SubtitleUnavailable as exc:
            raise SubtitlesUnavailable(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - adapter boundary: classify anything else per item
            _raise_unexpected(exc)
        return [
            SubtitleTrack(t["language"], t["language_code"], t["is_generated"], t["is_translatable"])
            for t in tracks
        ]

    def fetch(self, video_id: str, languages=None, preference="any", allow_translation=True) -> FetchedSubtitle:
        mod = self._module()
        try:
            transcript, translated, snippets = mod.fetch_selected(
                video_id, languages or ["original"], preference, allow_translation
            )
        except mod.LanguageUnavailable as exc:
            raise LanguageUnavailable(str(exc)) from exc
        except mod.SubtitleUnavailable as exc:
            raise SubtitlesUnavailable(str(exc)) from exc
        except mod.BlockedByYouTube as exc:
            raise BlockedByProvider(str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - adapter boundary: classify anything else per item
            _raise_unexpected(exc)
        return FetchedSubtitle(
            language=transcript.language,
            language_code=transcript.language_code,
            is_generated=transcript.is_generated,
            is_translatable=transcript.is_translatable,
            translated=translated,
            snippets=[SubtitleSnippet(s["text"], s["start"], s.get("duration", 0.0)) for s in snippets],
        )