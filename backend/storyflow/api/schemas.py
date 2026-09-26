"""Request models and id/param types for the HTTP API.

All request bodies use ``extra="forbid"`` and bounded fields. ``config`` (create workflow) must be
JSON-safe, at most 64 KiB serialized and 8 levels deep, and NO key at any depth may look like a secret
(``secret|token|password|api[_-]?key|credential``, case-insensitive): such requests are REJECTED with a
validation error, so secrets can never be persisted through the API. The rejection message never echoes
the offending key or value.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
PathId = Annotated[str, Path(min_length=1, max_length=64, pattern=ID_PATTERN)]
QueryId = Annotated[str | None, Query(min_length=1, max_length=64, pattern=ID_PATTERN)]
BodyId = Annotated[str, Field(min_length=1, max_length=64, pattern=ID_PATTERN)]

MAX_CONFIG_BYTES = 64 * 1024
MAX_CONFIG_DEPTH = 8
SECRET_KEY = re.compile(r"(secret|token|password|api[_-]?key|credential)", re.IGNORECASE)


def _walk(value: Any, depth: int) -> None:
    if depth > MAX_CONFIG_DEPTH:
        raise ValueError("config is nested too deeply")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError("config keys must be strings")
            if SECRET_KEY.search(k):
                raise ValueError("config must not contain secret-like keys")
            _walk(v, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _walk(v, depth + 1)
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("config must be JSON-safe")


def validate_config(value: dict) -> dict:
    _walk(value, 1)
    try:
        text = json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError("config must be JSON-safe") from None
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError("config is too large")
    return value


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)


class CreateWorkflowBody(_Body):
    name: str = Field(min_length=1, max_length=128)
    mode: str = Field(default="auto", min_length=1, max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    all_agents_unavailable_policy: str = Field(default="pause_auto_resume", max_length=32)
    role_preferences: dict[str, list[Annotated[str, Field(max_length=64)]]] | None = Field(default=None, max_length=16)
    client_key: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("config")
    @classmethod
    def _config_ok(cls, v: dict) -> dict:
        return validate_config(v)

    @field_validator("role_preferences")
    @classmethod
    def _prefs_ok(cls, v):
        if v is not None:
            for types in v.values():
                if len(types) > 16:
                    raise ValueError("too many runner types")
        return v


class AddProjectBody(_Body):
    title: str = Field(min_length=1, max_length=255)
    slug: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=4000)


class AddSourcesBody(_Body):
    """Links to expand into projects: videos, playlists, channels or ``inbox:<file>`` (see storyflow/sources.py)."""

    sources: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(min_length=1, max_length=50)
    limit: int | None = Field(default=10, ge=1, le=1000)          # newest N videos per channel / playlist
    languages: list[Annotated[str, Field(min_length=1, max_length=16)]] | None = Field(
        default=None, min_length=1, max_length=8)
    reprocess: bool = False                                        # add videos even if they were processed before
    min_duration_seconds: int | None = Field(default=None, ge=0, le=86400)


class RetryBody(_Body):
    project_id: BodyId | None = None


class AssignRunnerBody(_Body):
    workflow_id: BodyId | None = None
    session_id: BodyId | None = None
    roles: list[Annotated[str, Field(min_length=1, max_length=64)]] | None = Field(default=None, max_length=16)
