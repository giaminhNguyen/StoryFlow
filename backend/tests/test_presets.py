"""storyflow.presets: Fast / Balanced / Quality as plain configuration (pure functions) and how a new workflow
records them (WorkflowService.create_workflow)."""

import pytest

from storyflow.errors import ValidationFailed
from storyflow.presets import (
    DEFAULT_PRESET, MAX_ROUNDS, PRESETS, PresetError, ReviewSettings, apply_preset, parse_review_settings,
    review_from_config, validate_preset_name,
)


def test_presets_map_to_review_settings():
    fast, balanced, quality = (parse_review_settings(PRESETS[n]["review"]) for n in ("fast", "balanced", "quality"))
    assert fast == ReviewSettings(False, False, 1) and DEFAULT_PRESET == "fast"
    assert balanced == ReviewSettings(True, False, 1)          # verdict + issues recorded, story untouched
    assert quality == ReviewSettings(True, True, 2)            # reviewer corrects the story, up to 2 rounds
    assert MAX_ROUNDS >= quality.max_rounds


def test_engine_default_is_no_review():
    assert parse_review_settings(None) == ReviewSettings()
    assert review_from_config({}) == ReviewSettings() and review_from_config(None) == ReviewSettings()


@pytest.mark.parametrize("raw", [
    "on", ["x"], {"bogus": 1}, {"enabled": "yes"}, {"revise": 1}, {"max_rounds": 0}, {"max_rounds": MAX_ROUNDS + 1},
    {"max_rounds": True}, {"max_rounds": 1.5}, {"max_rounds": "2"},
])
def test_invalid_review_settings_are_rejected(raw):
    with pytest.raises(PresetError):
        parse_review_settings(raw)


def test_partial_review_settings_fill_in_defaults():
    assert parse_review_settings({"enabled": True}) == ReviewSettings(True, False, 1)
    assert parse_review_settings({"max_rounds": 3}) == ReviewSettings(False, False, 3)


@pytest.mark.parametrize("bad", ["", "turbo", None, 3, ["fast"]])
def test_unknown_preset_names_are_rejected(bad):
    with pytest.raises(PresetError):
        validate_preset_name(bad)


def test_review_from_config_explicit_block_beats_the_preset():
    assert review_from_config({"preset": "quality"}) == ReviewSettings(True, True, 2)
    assert review_from_config({"preset": "quality", "review": {"enabled": False}}) == ReviewSettings()
    assert review_from_config({"preset": "nope"}) == ReviewSettings()


def test_review_from_config_is_lenient_at_runtime():
    assert review_from_config({"review": {"enabled": "garbage"}}) == ReviewSettings()   # never crashes the loop
    assert review_from_config({"review": "garbage"}) == ReviewSettings()


def test_apply_preset_records_what_applies():
    assert apply_preset({}) == {"preset": "fast", "review": PRESETS["fast"]["review"]}
    out = apply_preset({"preset": "quality", "x": 1})
    assert out == {"preset": "quality", "x": 1, "review": PRESETS["quality"]["review"]}
    assert out["review"] is not PRESETS["quality"]["review"]                # a copy, never the shared dict
    explicit = apply_preset({"preset": "quality", "review": {"enabled": True, "revise": False}})
    assert explicit["review"] == {"enabled": True, "revise": False}          # explicit wins, kept as given
    assert apply_preset({"preset": None})["preset"] == "fast"


def test_apply_preset_rejects_bad_values_without_mutating_the_input():
    cfg = {"preset": "quality", "review": {"max_rounds": 99}}
    with pytest.raises(PresetError):
        apply_preset(cfg)
    assert cfg == {"preset": "quality", "review": {"max_rounds": 99}}
    with pytest.raises(PresetError):
        apply_preset({"preset": "turbo"})


# --- create_workflow ------------------------------------------------------------------------


@pytest.fixture
def workflows(session_factory, tmp_path):
    from datetime import datetime

    from storyflow.artifacts import ArtifactStore
    from storyflow.orchestrator import Orchestrator
    from storyflow.pipeline import PipelineContext
    from storyflow.services import WorkflowService
    from storyflow.story_steps import SourceStep
    ctx = PipelineContext(session_factory=session_factory, store=ArtifactStore(tmp_path / "a"), subtitle_client=None,
                          clock=lambda: datetime(2026, 1, 1))
    return WorkflowService(ctx, Orchestrator(ctx, dispatcher=None, source=SourceStep(), steps=[]))


def _config(workflows, wf_id):
    from storyflow.models import ChannelWorkflow
    with workflows.ctx.session_factory() as db:
        return db.get(ChannelWorkflow, wf_id).config


def test_create_workflow_defaults_to_the_fast_preset(workflows):
    cfg = _config(workflows, workflows.create_workflow("w").workflow_id)
    assert cfg["preset"] == "fast" and cfg["review"] == {"enabled": False, "revise": False, "max_rounds": 1}


def test_create_workflow_applies_the_named_preset(workflows):
    cfg = _config(workflows, workflows.create_workflow("w", config={"preset": "quality"}).workflow_id)
    assert cfg["preset"] == "quality" and cfg["review"] == {"enabled": True, "revise": True, "max_rounds": 2}


@pytest.mark.parametrize("bad", [{"preset": "turbo"}, {"review": {"bogus": 1}}, {"review": {"max_rounds": 9}},
                                 {"preset": 5}])
def test_create_workflow_rejects_a_bad_preset(workflows, bad):
    with pytest.raises(ValidationFailed) as exc:
        workflows.create_workflow("w", config=bad)
    assert exc.value.details["reason"] == "invalid_preset"
