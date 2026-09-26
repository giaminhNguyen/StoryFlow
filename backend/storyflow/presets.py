"""Workflow presets (roadmap 5.2): Fast / Balanced / Quality as plain configuration, not hard-coded UI logic.

A preset only chooses how much quality control runs after the story is written::

    fast      story -> TTS -> audio                                  (no review: cheapest, the default)
    balanced  story -> review (verdict + issues recorded) -> TTS -> audio
    quality   story -> review -> revised story (up to 2 rounds) -> TTS -> audio

They map onto the workflow config block ``review`` = ``{"enabled", "revise", "max_rounds"}``; ``preset`` itself is
only recorded for humans. An explicit ``review`` block always wins over the preset. Legacy workflows (no
``review`` block) behave like ``fast``. A review is ONE model call covering canon, logic, style and length
(a "team" of parallel reviewers would need more than one runner); when ``revise`` is on the same call returns
the corrected story, which becomes the newest StoryVersion that TTS reads.

Pure functions; nothing here touches the database.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PRESET = "fast"

PRESETS: dict[str, dict] = {
    "fast": {"review": {"enabled": False, "revise": False, "max_rounds": 1}},
    "balanced": {"review": {"enabled": True, "revise": False, "max_rounds": 1}},
    "quality": {"review": {"enabled": True, "revise": True, "max_rounds": 2}},
}

MAX_ROUNDS = 3


class PresetError(ValueError):
    """Invalid ``preset`` / ``review`` configuration (message is safe to show; never echoes user data)."""


@dataclass(frozen=True)
class ReviewSettings:
    enabled: bool = False
    revise: bool = False        # the reviewer also returns a corrected story (a new StoryVersion)
    max_rounds: int = 1         # review rounds per story (each round after a revision reviews the new version)


def parse_review_settings(raw) -> ReviewSettings:
    """Strict parse used when a workflow is created: unknown keys / bad values raise PresetError."""
    if raw is None:
        return ReviewSettings()
    if not isinstance(raw, dict):
        raise PresetError("review must be an object")
    unknown = sorted(set(raw) - {"enabled", "revise", "max_rounds"})
    if unknown:
        raise PresetError("review has unknown keys: " + ", ".join(str(k)[:40] for k in unknown))
    kw: dict = {}
    for key in ("enabled", "revise"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise PresetError(f"review.{key} must be true or false")
            kw[key] = raw[key]
    if "max_rounds" in raw:
        v = raw["max_rounds"]
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= MAX_ROUNDS:
            raise PresetError(f"review.max_rounds must be a whole number between 1 and {MAX_ROUNDS}")
        kw["max_rounds"] = v
    return ReviewSettings(**kw)


def validate_preset_name(name) -> str:
    if not isinstance(name, str) or name not in PRESETS:
        raise PresetError("preset must be one of: " + ", ".join(PRESETS))
    return name


def review_from_config(config) -> ReviewSettings:
    """Lenient read used while running: an explicit ``review`` block, else the named preset, else disabled.
    Invalid stored values degrade to 'no review' instead of crashing the orchestrator loop."""
    if not isinstance(config, dict):
        return ReviewSettings()
    try:
        if "review" in config:
            return parse_review_settings(config["review"])
        preset = config.get("preset")
        if preset in PRESETS:
            return parse_review_settings(PRESETS[preset]["review"])
    except PresetError:
        pass
    return ReviewSettings()


def apply_preset(config: dict) -> dict:
    """Validate ``preset`` / ``review`` of a new workflow config and record what applies (explicit ``review``
    wins). Returns a new dict; raises PresetError."""
    preset = validate_preset_name(config["preset"]) if "preset" in config and config["preset"] is not None \
        else DEFAULT_PRESET
    parse_review_settings(config.get("review"))
    out = {**config, "preset": preset}
    if "review" not in out:
        out["review"] = dict(PRESETS[preset]["review"])
    return out
