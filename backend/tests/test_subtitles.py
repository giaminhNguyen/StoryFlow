"""SubtitleClient abstraction tests: deterministic fake + real-adapter mapping.

No external deps are required: ExternalSubtitleClient is exercised with a scripted
provider module, so the external subtitle_suppervip package never has to be installed.
"""

import types

import pytest

from storyflow.subtitles import (
    BlockedByProvider,
    ExternalSubtitleClient,
    FakeSubtitleClient,
    LanguageUnavailable,
    SubtitleTrack,
    SubtitlesUnavailable,
    plain_text,
)


def track(lang, code, generated=False, translatable=False, snippets=None):
    return {
        "language": lang,
        "language_code": code,
        "is_generated": generated,
        "is_translatable": translatable,
        "snippets": snippets or [{"text": f"hello-{code}", "start": 0.0, "duration": 1.25}],
    }


STORE = {
    "video-vi": {
        "tracks": [
            track("English", "en", generated=True, translatable=True),
            track("Tiếng Việt", "vi", generated=False, translatable=True),
        ]
    },
    "video-en": {"tracks": [track("English", "en", generated=False, translatable=True)]},
    "video-none": {"error": "no_subtitle"},
    "video-blocked": {"error": "blocked"},
}


@pytest.fixture
def client():
    return FakeSubtitleClient(STORE)


# --- fake: list_tracks ------------------------------------------------------


def test_list_tracks_returns_tracks_in_order(client):
    tracks = client.list_tracks("video-vi")
    assert tracks == [
        SubtitleTrack("English", "en", True, True),
        SubtitleTrack("Tiếng Việt", "vi", False, True),
    ]


def test_list_tracks_no_subtitle_raises(client):
    with pytest.raises(SubtitlesUnavailable):
        client.list_tracks("video-none")


def test_list_tracks_missing_video_raises(client):
    with pytest.raises(SubtitlesUnavailable):
        client.list_tracks("nope")


def test_list_tracks_blocked_raises(client):
    with pytest.raises(BlockedByProvider):
        client.list_tracks("video-blocked")


# --- fake: fetch selection --------------------------------------------------


def test_fetch_prefers_requested_language_over_first(client):
    out = client.fetch("video-vi", languages=["vi", "en"], preference="any", allow_translation=True)
    assert out.language_code == "vi"
    assert not out.translated
    assert [s.text for s in out.snippets] == ["hello-vi"]


def test_fetch_manual_preference_skips_generated(client):
    # generated "en" is first, but manual preference wants hand-made captions -> "vi"
    out = client.fetch("video-vi", languages=["vi", "en"], preference="manual", allow_translation=True)
    assert out.language_code == "vi"
    assert not out.is_generated


def test_fetch_manual_only_track_translates_when_language_missing(client):
    # manual leaves only hand-made "vi"; the requested "en" is reached via translation
    out = client.fetch("video-vi", languages=["en"], preference="manual", allow_translation=True)
    assert out.translated
    assert out.language_code == "en"
    assert not out.is_generated


def test_fetch_auto_preference_prefers_generated(client):
    out = client.fetch("video-vi", languages=["vi", "en"], preference="auto", allow_translation=True)
    assert out.language_code == "en"
    assert out.is_generated


def test_fetch_original_returns_first_track(client):
    out = client.fetch("video-vi", languages=["original"])
    assert out.language_code == "en"


def test_fetch_matches_base_language_code(client):
    out = client.fetch("video-vi", languages=["en-US"], preference="any")
    assert out.language_code == "en"


def test_fetch_translation_fallback_when_language_missing(client):
    # only one track is translatable; requested languages don't exist natively
    out = client.fetch("video-en", languages=["vi"], allow_translation=True)
    assert out.translated
    assert out.language_code == "vi"


def test_fetch_language_unavailable_without_translation(client):
    with pytest.raises(LanguageUnavailable):
        client.fetch("video-en", languages=["vi"], allow_translation=False)


def test_fetch_language_unavailable_nontranslatable_track(client):
    fixed = FakeSubtitleClient({"video-x": {"tracks": [track("English", "en", translatable=False)]}})
    with pytest.raises(LanguageUnavailable):
        fixed.fetch("video-x", languages=["vi"], allow_translation=True)


def test_fetch_no_subtitle_raises(client):
    with pytest.raises(SubtitlesUnavailable):
        client.fetch("video-none")


def test_fetch_is_deterministic(client):
    first = client.fetch("video-vi", languages=["en"], preference="auto")
    second = client.fetch("video-vi", languages=["en"], preference="auto")
    assert first == second


def test_plain_text_joins_snippet_text(client):
    out = client.fetch("video-vi", languages=["vi"])
    assert plain_text(out) == "hello-vi"


# --- external adapter mapping -----------------------------------------------


def stub_provider(**overrides):
    """A scripted stand-in for app.services.subtitles (no external package needed)."""
    base = types.SimpleNamespace(
        LanguageUnavailable=LanguageUnavailable,
        SubtitlesUnavailable=SubtitlesUnavailable,
        BlockedByYouTube=BlockedByProvider,
    )
    return types.SimpleNamespace(**{**vars(base), **overrides})


def test_external_client_imports_provider_lazily():
    seen = []
    provider = stub_provider(
        available_transcripts=lambda video_id: [
            {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True}
        ]
    )
    client = ExternalSubtitleClient(provider_builder=lambda path: seen.append(path) or provider)
    assert not seen, "provider must not load until first use"
    tracks = client.list_tracks("v1")
    assert seen and tracks == [SubtitleTrack("English", "en", False, True)]


def test_external_client_maps_fetch_result():
    provider = stub_provider(
        fetch_selected=lambda video_id, languages, preference, allow_translation: (
            types.SimpleNamespace(language="English", language_code="en", is_generated=False, is_translatable=True),
            False,
            [{"text": "hi", "start": 0.0, "duration": 1.5}],
        )
    )
    client = ExternalSubtitleClient(provider_builder=lambda path: provider)
    out = client.fetch("v1", languages=["en"])
    assert out.language_code == "en"
    assert [s.text for s in out.snippets] == ["hi"]


def test_external_client_maps_provider_errors():
    def raising(exception):
        return lambda video_id: (_ for _ in ()).throw(exception)

    provider = stub_provider(
        available_transcripts=raising(BlockedByProvider("blocked")),
        fetch_selected=lambda *a, **k: (_ for _ in ()).throw(LanguageUnavailable("no lang")),
    )
    client = ExternalSubtitleClient(provider_builder=lambda path: provider)
    with pytest.raises(BlockedByProvider):
        client.list_tracks("v1")
    with pytest.raises(LanguageUnavailable):
        client.fetch("v1", languages=["vi"])


def test_external_client_import_failure_has_actionable_message(tmp_path):
    from storyflow.subtitles import import_external_subtitles

    with pytest.raises(ImportError, match="external subtitle provider not importable"):
        import_external_subtitles(tmp_path)