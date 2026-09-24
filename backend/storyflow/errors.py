"""Stable domain errors for the application layer (Phase 5).

Every error carries a machine-readable ``code`` so the Phase 6 HTTP adapter can map it to a
status code without string matching. Services raise these; nothing else escapes on expected
failures. ``details`` is a small JSON-safe dict (ids, states) and never holds secrets, claim
tokens or filesystem paths.
"""


class StoryFlowError(Exception):
    code = "internal"

    def __init__(self, message: str = "", **details):
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details


class ValidationFailed(StoryFlowError):
    code = "validation"


class NotFound(StoryFlowError):
    code = "not_found"


class Conflict(StoryFlowError):
    """The request is valid but collides with current durable state (e.g. runner already
    assigned to another session, duplicate slug)."""

    code = "conflict"


class InvalidState(StoryFlowError):
    """The command is not allowed from the current lifecycle state (e.g. start a cancelled
    workflow, resume a workflow with failed steps)."""

    code = "invalid_state"


class NotRetryable(StoryFlowError):
    """Retry requested but nothing is in a retryable (failed) state."""

    code = "not_retryable"


class CapacityUnavailable(StoryFlowError):
    code = "capacity_unavailable"
