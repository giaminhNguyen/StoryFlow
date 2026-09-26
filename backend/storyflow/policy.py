"""Failure policy for batch runs (roadmap 4.2 / 4.6 / 5.1): what to do when ONE item fails.

Stored in the workflow config under ``failure_policy`` (all keys optional)::

    {"subtitle_retries": 5,          # transient source errors tolerated before giving up (None = unlimited)
     "retry_base_seconds": 30,       # exponential backoff: base * 2**(attempt-1), capped
     "retry_max_seconds": 900,
     "on_no_subtitle": "skip",       # pause | skip      video has no usable subtitle in the wanted language
     "on_permanent_error": "continue"}  # pause | continue  an item failed for good (retries used up, ...)

``pause`` (the engine default, the historic behaviour) stops the whole workflow until an operator
retries or resumes it. ``skip`` / ``continue`` mark ONLY the affected project (``skipped`` /
``needs_attention``) and let the rest of the batch carry on.

Operator errors (a missing provider install, an unreadable config) always pause: they would fail
every item the same way, so continuing would only burn the whole batch.

``batch`` (workflow config, roadmap 4.3) bounds how many projects run at once::

    {"max_active": 2}    # only the first 2 unfinished projects (creation order) are advanced; None = all (legacy)

Finished, skipped and needs-attention projects free their slot, so the next video starts right away and
projects overlap across pipeline stages (one is writing a story while another is being read aloud) without
hitting the subtitle provider for the whole list at once.

Error taxonomy for the source step (``classify_source_error``)::

    transient   provider_blocked, provider_timeout      -> retry later with backoff (never a batch failure)
    no_subtitle subtitles_unavailable, language_unavailable, empty_source
                                                       -> ``on_no_subtitle``
    permanent   subtitle_retries_exhausted, source_not_configured, snapshot_contention, ...
                                                       -> ``on_permanent_error``
    operator    provider_unavailable                    -> always pause

Pure functions only: nothing here touches the database.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ProjectStatus

ON_NO_SUBTITLE = ("pause", "skip")
ON_PERMANENT = ("pause", "continue")

TRANSIENT, NO_SUBTITLE, PERMANENT, OPERATOR = "transient", "no_subtitle", "permanent", "operator"

_TRANSIENT_CODES = frozenset({"provider_blocked", "provider_timeout"})
_NO_SUBTITLE_CODES = frozenset({"subtitles_unavailable", "language_unavailable", "empty_source"})
_OPERATOR_CODES = frozenset({"provider_unavailable"})

# Recommended values written into new workflows (the engine default stays "legacy": pause, no backoff).
RECOMMENDED = {
    "subtitle_retries": 5,
    "retry_base_seconds": 30,
    "retry_max_seconds": 900,
    "on_no_subtitle": "pause",
    "on_permanent_error": "pause",
}
# Recommended for multi-video runs: a bad item must not stop the rest.
RECOMMENDED_BATCH = {**RECOMMENDED, "on_no_subtitle": "skip", "on_permanent_error": "continue"}

RECOMMENDED_BATCH_SETTINGS = {"max_active": 2}

_MAX_RETRIES = 100
_MAX_SECONDS = 24 * 3600
_MAX_ACTIVE = 50


class PolicyError(ValueError):
    """Invalid ``failure_policy`` (message is safe to show; never echoes user data)."""


@dataclass(frozen=True)
class FailurePolicy:
    subtitle_retries: int | None = None      # None = unlimited (legacy)
    retry_base_seconds: float = 0.0          # 0 = retry on the next tick (legacy)
    retry_max_seconds: float = 900.0
    on_no_subtitle: str = "pause"
    on_permanent_error: str = "pause"

    def backoff_seconds(self, attempts: int) -> float:
        """Delay before the retry that follows the ``attempts``-th consecutive transient failure."""
        if self.retry_base_seconds <= 0 or attempts < 1:
            return 0.0
        return float(min(self.retry_base_seconds * (2 ** min(attempts - 1, 30)), self.retry_max_seconds))

    def exhausted(self, attempts: int) -> bool:
        return self.subtitle_retries is not None and attempts >= self.subtitle_retries


@dataclass(frozen=True)
class BatchSettings:
    max_active: int | None = None            # None = every project at once (legacy)


def parse_batch_settings(raw) -> BatchSettings:
    """Strict parse used when a workflow is created: unknown keys / bad values raise PolicyError."""
    if raw is None:
        return BatchSettings()
    if not isinstance(raw, dict):
        raise PolicyError("batch must be an object")
    unknown = sorted(set(raw) - {"max_active"})
    if unknown:
        raise PolicyError("batch has unknown keys: " + ", ".join(str(k)[:40] for k in unknown))
    value = raw.get("max_active")
    if value is None:
        return BatchSettings()
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_ACTIVE:
        raise PolicyError(f"batch.max_active must be a whole number between 1 and {_MAX_ACTIVE}")
    return BatchSettings(value)


def batch_from_config(config) -> BatchSettings:
    """Lenient read used while running: invalid stored settings degrade to the legacy default."""
    raw = config.get("batch") if isinstance(config, dict) else None
    try:
        return parse_batch_settings(raw)
    except PolicyError:
        return BatchSettings()


def _num(value, name, *, minimum, maximum, integer):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise PolicyError(f"failure_policy.{name} must be a number")
    if integer and int(value) != value:
        raise PolicyError(f"failure_policy.{name} must be a whole number")
    if not minimum <= value <= maximum:
        raise PolicyError(f"failure_policy.{name} must be between {minimum} and {maximum}")
    return int(value) if integer else float(value)


def parse_failure_policy(raw) -> FailurePolicy:
    """Strict parse used when a workflow is created: unknown keys / bad values raise PolicyError."""
    if raw is None:
        return FailurePolicy()
    if not isinstance(raw, dict):
        raise PolicyError("failure_policy must be an object")
    unknown = sorted(set(raw) - set(RECOMMENDED))
    if unknown:
        raise PolicyError("failure_policy has unknown keys: " + ", ".join(str(k)[:40] for k in unknown))
    kw: dict = {}
    if "subtitle_retries" in raw:
        v = raw["subtitle_retries"]
        kw["subtitle_retries"] = None if v is None else _num(v, "subtitle_retries", minimum=1, maximum=_MAX_RETRIES,
                                                             integer=True)
    if "retry_base_seconds" in raw:
        kw["retry_base_seconds"] = _num(raw["retry_base_seconds"], "retry_base_seconds", minimum=0,
                                        maximum=_MAX_SECONDS, integer=False)
    if "retry_max_seconds" in raw:
        kw["retry_max_seconds"] = _num(raw["retry_max_seconds"], "retry_max_seconds", minimum=0,
                                       maximum=_MAX_SECONDS, integer=False)
    if "on_no_subtitle" in raw:
        if raw["on_no_subtitle"] not in ON_NO_SUBTITLE:
            raise PolicyError("failure_policy.on_no_subtitle must be one of: " + ", ".join(ON_NO_SUBTITLE))
        kw["on_no_subtitle"] = raw["on_no_subtitle"]
    if "on_permanent_error" in raw:
        if raw["on_permanent_error"] not in ON_PERMANENT:
            raise PolicyError("failure_policy.on_permanent_error must be one of: " + ", ".join(ON_PERMANENT))
        kw["on_permanent_error"] = raw["on_permanent_error"]
    return FailurePolicy(**kw)


def policy_from_config(config) -> FailurePolicy:
    """Lenient read used while running: a stored-but-invalid policy degrades to the legacy default
    instead of crashing the orchestrator loop."""
    raw = config.get("failure_policy") if isinstance(config, dict) else None
    try:
        return parse_failure_policy(raw)
    except PolicyError:
        return FailurePolicy()


def classify_source_error(code: str | None) -> str:
    code = (code or "").lower()
    if code in _TRANSIENT_CODES:
        return TRANSIENT
    if code in _NO_SUBTITLE_CODES:
        return NO_SUBTITLE
    if code in _OPERATOR_CODES:
        return OPERATOR
    return PERMANENT


def terminal_status_for(policy: FailurePolicy, category: str) -> str | None:
    """Project status to apply for a failure of ``category``, or None to pause the workflow."""
    if category == NO_SUBTITLE and policy.on_no_subtitle == "skip":
        return ProjectStatus.SKIPPED.value
    if category == PERMANENT and policy.on_permanent_error == "continue":
        return ProjectStatus.NEEDS_ATTENTION.value
    return None
