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


# --- STORYFLOW_LISTER_TIMEOUT ---------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["abc", "", " ", "0", "-5", "nan", "inf", "1e999", "12s"])
def test_invalid_lister_timeout_falls_back_to_the_default_and_is_reported(raw, tmp_path, caplog):
    import logging
    cfg = load_provider_config(env={"STORYFLOW_LISTER_TIMEOUT": raw}, env_files=[])
    if raw.strip() == "":                       # unset / blank means "not configured": default, nothing to report
        assert cfg.lister_timeout == 120.0 and cfg.problems() == []
        return
    assert cfg.lister_timeout == 120.0 and cfg.lister_timeout_invalid is True
    assert any("lister_timeout" in p and "STORYFLOW_LISTER_TIMEOUT" in p for p in cfg.problems())
    with caplog.at_level(logging.WARNING, logger="storyflow.providers"):
        stack = build_provider_stack(cfg, ArtifactStore(tmp_path))
    assert stack.video_lister.timeout == 120.0                       # never -1: no instant "timeout"
    assert any("lister_timeout" in w for w in stack.warnings)
    assert any("STORYFLOW_LISTER_TIMEOUT" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("raw,expected", [("30", 30.0), (" 45.5 ", 45.5), ("1e2", 100.0)])
def test_valid_lister_timeout_is_used_and_nothing_is_reported(raw, expected, tmp_path):
    cfg = load_provider_config(env={"STORYFLOW_LISTER_TIMEOUT": raw}, env_files=[])
    assert cfg.lister_timeout == expected and not cfg.lister_timeout_invalid and cfg.problems() == []
    stack = build_provider_stack(cfg, ArtifactStore(tmp_path))
    assert stack.video_lister.timeout == expected and stack.warnings == []


def test_directly_constructed_bad_timeout_is_still_a_problem():
    assert any("lister_timeout" in p for p in ProviderConfig(lister_timeout=-1).problems())


# --- fake story provider serves the review step ---------------------------------------------------


def _fake_runtime(tmp_path):
    from storyflow.readmodels import ReadModels
    from storyflow.services import RunnerService, WorkflowService
    cfg = ProviderConfig(subtitle_provider="fake", story_runner="fake", tts_engine="fake")
    app = build_runtime(database_url=f"sqlite:///{(tmp_path / 'p.db').as_posix()}", artifact_root=tmp_path / "a",
                        provider_config=cfg, ensure_db_schema=True, sleep=lambda s: None)
    read = ReadModels(app.ctx, app.orchestrator.chain)
    return app, WorkflowService(app.ctx, app.orchestrator), RunnerService(app.session_factory, app.ctx.clock), read


def _run(app, workflows, runners, read, preset, verdicts=None):
    wid = workflows.create_workflow("w", config={"source": {"video_id": "demo-video", "languages": ["en"]},
                                                 "preset": preset}).workflow_id
    workflows.add_project(wid, "P")
    if verdicts is not None:
        story_provider = next(p for p in app.providers if getattr(p, "name", "") == "fake-story")
        story_provider.runners["fake-story-1"].review.verdicts = list(verdicts)
    workflows.start(wid)
    app.supervisor.refresh()
    for runner in read.list_runners(unassigned=True):
        runners.assign_runner(runner.id, workflow_id=wid)
    for _ in range(80):
        app.runtime.run_once()
        snap = read.get_workflow(wid)
        if snap.status in ("finished", "paused"):
            break
    return read.get_workflow(wid)


def test_fake_story_runner_keeps_its_identity_and_serves_canon_story_and_review(tmp_path):
    from storyflow.story_steps import FakeStoryPipelineRunner
    stack = build_provider_stack(ProviderConfig(subtitle_provider="none", story_runner="fake"), ArtifactStore(tmp_path))
    (provider,) = stack.runner_providers
    assert provider.name == "fake-story" and provider.roles == ["story_writer"] and list(provider.runners) == ["fake-story-1"]
    runner = provider.runners["fake-story-1"]
    assert isinstance(runner, FakeStoryPipelineRunner) and runner.runner_type == "fake_story"


@pytest.mark.parametrize("preset,expect_review", [("fast", False), ("balanced", True), ("quality", True)])
def test_every_preset_finishes_on_the_fake_stack(tmp_path, preset, expect_review):
    app, workflows, runners, read = _fake_runtime(tmp_path)
    try:
        snap = _run(app, workflows, runners, read, preset)
        assert snap.status == "finished", (snap.status, snap.status_reason, snap.status_detail)
        project = snap.projects[0]
        steps = [s.step for s in project.steps]
        assert ("review" in steps) is expect_review
        if expect_review:
            assert project.review is not None and project.review.status == "completed"
            assert project.review.verdict == "approve"
    finally:
        app.close()


def test_quality_preset_on_the_fake_stack_revises_and_tts_reads_the_new_version(tmp_path):
    app, workflows, runners, read = _fake_runtime(tmp_path)
    try:
        snap = _run(app, workflows, runners, read, "quality", verdicts=["revise", "approve"])
        assert snap.status == "finished", (snap.status, snap.status_reason, snap.status_detail)
        project = snap.projects[0]
        assert project.story_version.version_number == 2 and project.revision_count == 1
        assert project.review.round_number == 2 and project.review.verdict == "approve"
        from sqlalchemy import select

        from storyflow.models import StoryVersion, TTSGeneration
        with app.session_factory() as db:
            newest = db.scalar(select(StoryVersion).where(StoryVersion.story_project_id == project.id)
                               .order_by(StoryVersion.version_number.desc()))
            tts = db.scalar(select(TTSGeneration))
            assert tts.story_version_id == newest.id and newest.version_number == 2
    finally:
        app.close()
