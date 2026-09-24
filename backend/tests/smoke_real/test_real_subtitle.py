"""Opt-in REAL subtitle smoke (network). Skipped unless STORYFLOW_RUN_REAL_SMOKE=1.

    STORYFLOW_RUN_REAL_SMOKE=1 STORYFLOW_SUBTITLE_PYTHON=<python with requirements-subtitle.txt> pytest tests/smoke_real
"""

import os

import pytest

from storyflow.integrations.subtitle_subprocess import SubprocessSubtitleClient
from storyflow.providers import load_provider_config
from storyflow.subtitles import BlockedByProvider, ProviderUnavailable

pytestmark = pytest.mark.skipif(
    os.environ.get("STORYFLOW_RUN_REAL_SMOKE") != "1",
    reason="real subtitle smoke is opt-in: set STORYFLOW_RUN_REAL_SMOKE=1 (uses the network)")

VIDEO_ID = "dQw4w9WgXcQ"


def test_real_list_and_fetch():
    client = SubprocessSubtitleClient(load_provider_config())
    try:
        tracks = client.list_tracks(VIDEO_ID)
        assert tracks
        fetched = client.fetch(VIDEO_ID, [tracks[0].language_code], "any", False)
    except BlockedByProvider:
        pytest.skip("provider blocked")
    except ProviderUnavailable as exc:
        pytest.fail(f"provider not usable: {exc}")
    assert fetched.snippets and fetched.snippets[0].text
