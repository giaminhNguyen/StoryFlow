"""Workflow presets (roadmap 5.2): Fast / Balanced / Quality as plain configuration, not hard-coded UI logic.

A preset only chooses how much quality control runs after the story is written::

    fast      story -> TTS -> audio                                  (no review: cheapest, the default)
    balanced  story -> review (verdict + issues recorded) -> TTS -> audio; a failed review never blocks TTS
    quality   story -> review -> revised story (up to 2 rounds) -> TTS -> audio; a failed review blocks

They map onto the workflow config block ``review`` = ``{"enabled", "revise", "max_rounds", "on_failure"}``;
``preset`` itself is only recorded for humans. An explicit ``review`` block is OVERLAID on the preset's block
(``{**preset, **explicit}``), so ``{"preset": "quality", "review": {"max_rounds": 3}}`` is quality with three
rounds. Legacy workflows (no ``review`` block, no preset) behave like ``fast``. A review is ONE model call
covering canon, logic, style and length (a "team" of parallel reviewers would need more than one runner); when
``revise`` is on the same call returns the corrected story, which becomes the newest StoryVersion that TTS reads.

``on_failure`` says what a review that failed for good does: ``"block"`` (default; the project step fails like any
other step) or ``"skip"`` (the review is advisory: the failed round stays visible but the story goes on to TTS).

Pure functions; nothing here touches the database.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_PRESET = "fast"

ON_FAILURE = ("block", "skip")

PRESETS: dict[str, dict] = {
    "fast": {"review": {"enabled": False, "revise": False, "max_rounds": 1, "on_failure": "block"}},
    "balanced": {"review": {"enabled": True, "revise": False, "max_rounds": 1, "on_failure": "skip"}},
    "quality": {"review": {"enabled": True, "revise": True, "max_rounds": 2, "on_failure": "block"}},
}

MAX_ROUNDS = 3


class PresetError(ValueError):
    """Invalid ``preset`` / ``review`` configuration (message is safe to show; never echoes user data)."""


@dataclass(frozen=True)
class ReviewSettings:
    enabled: bool = False
    revise: bool = False        # the reviewer also returns a corrected story (a new StoryVersion)
    max_rounds: int = 1         # review rounds per story (each round after a revision reviews the new version)
    on_failure: str = "block"   # block | skip: what a review that failed for good does (skip = advisory)


def parse_review_settings(raw) -> ReviewSettings:
    """Strict parse used when a workflow is created: unknown keys / bad values raise PresetError."""
    if raw is None:
        return ReviewSettings()
    if not isinstance(raw, dict):
        raise PresetError("review must be an object")
    unknown = sorted(set(raw) - {"enabled", "revise", "max_rounds", "on_failure"})
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
    if "on_failure" in raw:
        if raw["on_failure"] not in ON_FAILURE:
            raise PresetError("review.on_failure must be one of: " + ", ".join(ON_FAILURE))
        kw["on_failure"] = raw["on_failure"]
    return ReviewSettings(**kw)


def validate_preset_name(name) -> str:
    if not isinstance(name, str) or name not in PRESETS:
        raise PresetError("preset must be one of: " + ", ".join(PRESETS))
    return name


def review_from_config(config) -> ReviewSettings:
    """Lenient read used while running: the named preset's block overlaid with an explicit ``review`` block
    (``"review": null`` is ignored: the preset applies), else disabled. Invalid stored values degrade to 'no
    review' instead of crashing the orchestrator loop."""
    if not isinstance(config, dict):
        return ReviewSettings()
    try:
        preset = config.get("preset")
        base = PRESETS[preset]["review"] if isinstance(preset, str) and preset in PRESETS else {}
        explicit = config.get("review")
        if explicit is None:
            return parse_review_settings(base)
        if not isinstance(explicit, dict):
            raise PresetError("review must be an object")
        return parse_review_settings({**base, **explicit})
    except PresetError:
        return ReviewSettings()


def apply_preset(config: dict) -> dict:
    """Validate ``preset`` / ``review`` of a new workflow config and record what applies: the preset's review
    block overlaid with the explicit one. Returns a new dict; raises PresetError (``"review": null`` is refused)."""
    preset = validate_preset_name(config["preset"]) if config.get("preset") is not None else DEFAULT_PRESET
    if "review" in config and config["review"] is None:
        raise PresetError("review must be an object")
    explicit = config.get("review")
    parse_review_settings(explicit)                       # strict: unknown keys / bad values
    merged = {**PRESETS[preset]["review"], **(explicit or {})}
    settings = parse_review_settings(merged)
    if (explicit or {}).get("revise") is True and not settings.enabled:
        raise PresetError("review.revise needs review.enabled (or a preset that enables the review)")
    return {**config, "preset": preset, "review": merged}
