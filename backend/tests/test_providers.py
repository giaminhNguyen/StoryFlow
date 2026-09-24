"""Phase 8 configuration + stack assembly + /api/providers (no real provider is ever started)."""

import pytest
from fastapi.testclient import TestClient

from storyflow.api.app import create_app
from storyflow.artifacts import ArtifactStore
from storyflow.providers import (
    DISABLED, FAKE, MISCONFIGURED, ProviderConfig, build_provider_stack, load_provider_config, parse_dotenv,
)
from storyflow.runtime import build_runtime


def test_parse_dotenv_handles_comments_quotes_and_blank_lines():
    text = "# c\n\nSTORYFLOW_A=1\nSTORYFLOW_B = 'two words'\nSTORYFLOW_C=x # trailing\nbad line\n=novalue\n"
    assert parse_dotenv(text) == {"STORYFLOW_A": "1", "STORYFLOW_B": "two words", "STORYFLOW_C": "x"}


def test_defaults_enable_no_real_story_or_tts_provider():
    cfg = load_provider_config(env={}, env_files=[])
    assert (cfg.subtitle_provider, cfg.story_runner, cfg.tts_engine) == ("external", "none", "none")
    assert cfg.problems() == []


def test_env_overrides_dotenv_and_values_are_normalised(tmp_path):
    envfile = tmp_path / ".env"
    envfile.write_text("STORYFLOW_STORY_RUNNER=none\nSTORYFLOW_TTS_ENGINE=VIENEU\nSTORYFLOW_VIENEU_THREADS=2\n",
                       encoding="utf-8")
    cfg = load_provider_config(env={"STORYFLOW_STORY_RUNNER": "Claude-CLI", "OTHER": "ignored"},
                               env_files=[envfile])
    assert cfg.story_runner == "claude-cli" and cfg.tts_engine == "vieneu" and cfg.vieneu_threads == 2


@pytest.mark.parametrize("env,needle", [
    ({"STORYFLOW_STORY_RUNNER": "gpt"}, "STORY_RUNNER"),
    ({"STORYFLOW_TTS_ENGINE": "polly"}, "TTS_ENGINE"),
    ({"STORYFLOW_SUBTITLE_PROVIDER": "x"}, "SUBTITLE_PROVIDER"),
    ({"STORYFLOW_VIENEU_PRECISION": "fp8"}, "PRECISION"),
    ({"STORYFLOW_VIENEU_THREADS": "many"}, "THREADS"),
    ({"STORYFLOW_STORY_TIMEOUT": "0"}, "story_timeout"),
])
def test_invalid_values_are_reported_not_raised(env, needle):
    cfg = load_provider_config(env=env, env_files=[])
    assert any(needle in p for p in cfg.problems())


def test_disabled_stack_reports_honestly_and_registers_no_runners(tmp_path):
    cfg = ProviderConfig(subtitle_provider="none", story_runner="none", tts_engine="none")
    stack = build_provider_stack(cfg, ArtifactStore(tmp_path))
    assert stack.runner_providers == [] and not stack.ready
    assert {(s.kind, s.state) for s in stack.statuses()} == {("subtitle", DISABLED), ("story", DISABLED), ("tts", DISABLED)}
    from storyflow.subtitles import ProviderUnavailable
    with pytest.raises(ProviderUnavailable):
        stack.subtitle_client.list_tracks("x")


def test_misconfigured_values_yield_misconfigured_status_and_no_provider(tmp_path):
    cfg = ProviderConfig(subtitle_provider="none", story_runner="gpt", tts_engine="polly")
    stack = build_provider_stack(cfg, ArtifactStore(tmp_path))
    states = {s.kind: s.state for s in stack.statuses()}
    assert states["story"] == MISCONFIGURED and states["tts"] == MISCONFIGURED
    assert stack.runner_providers == []


def test_fake_stack_is_ready_and_marked_fake_never_real(tmp_path):
    cfg = ProviderConfig(subtitle_provider="fake", story_runner="fake", tts_engine="fake")
    stack = build_provider_stack(cfg, ArtifactStore(tmp_path))
    assert stack.ready and {s.state for s in stack.statuses()} == {FAKE}
    assert sorted(p.name for p in stack.runner_providers) == ["fake-story", "fake-tts"]


def test_api_providers_endpoint_and_no_paths_or_secrets(tmp_path):
    rt = build_runtime(database_url=f"sqlite:///{(tmp_path / 'p.db').as_posix()}", artifact_root=tmp_path / "a",
                       ensure_db_schema=True, provider_config=ProviderConfig(
                           subtitle_provider="none", story_runner="none", tts_engine="fake"))
    try:
        client = TestClient(create_app(rt))
        body = client.get("/api/providers").json()
        assert body["configured"] is True and body["ready"] is False
        by_kind = {p["kind"]: p for p in body["providers"]}
        assert by_kind["subtitle"]["state"] == "disabled" and by_kind["tts"]["state"] == "fake"
        assert by_kind["tts"]["usable"] is True and by_kind["story"]["usable"] is False
        text = client.get("/api/providers").text
        assert str(tmp_path) not in text and "token" not in text.lower()
    finally:
        rt.close()


def test_api_providers_unconfigured_when_providers_are_injected(tmp_path):
    from storyflow.runtime import StaticRunnerProvider

    rt = build_runtime(database_url=f"sqlite:///{(tmp_path / 'p.db').as_posix()}", artifact_root=tmp_path / "a",
                       ensure_db_schema=True, providers=[StaticRunnerProvider({}, name="x")],
                       subtitle_client=object())
    try:
        assert TestClient(create_app(rt)).get("/api/providers").json() == {
            "configured": False, "ready": None, "providers": []}
    finally:
        rt.close()


def test_fake_runtime_reports_fake_providers(tmp_path):
    rt = build_runtime(database_url=f"sqlite:///{(tmp_path / 'p.db').as_posix()}", artifact_root=tmp_path / "a",
                       ensure_db_schema=True, fake=True)
    try:
        body = TestClient(create_app(rt)).get("/api/providers").json()
        assert body["ready"] is True and {p["state"] for p in body["providers"]} == {"fake"}
    finally:
        rt.close()
