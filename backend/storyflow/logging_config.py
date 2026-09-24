"""Process-wide logging setup for the server / runtime CLIs (Phase 9).

``configure_logging`` attaches ONE console handler (stderr) and, when asked, ONE rotating UTF-8 file
handler (``<log_dir>/storyflow.log``) to the root logger, so uvicorn / asyncio / alembic / storyflow
loggers all flow through the same handlers. It is idempotent (a second call replaces our handlers) and
``handle.close()`` removes them again and restores the previous root level.

Safety contract (there is deliberately NO switch that logs story text):

* every handler carries :class:`RedactingFilter`: secrets (``sk-`` keys, bearer tokens, ``Authorization:``
  values, ``api_key=``/``password=``/``token=`` values, long base64-ish blobs) are masked, absolute
  Windows/Unix paths become ``<path>``, tracebacks are redacted the same way, and a single message is
  truncated to ``MAX_MESSAGE_CHARS``;
* :class:`AccessLogFilter` drops the query string from uvicorn access lines (method + path only).
"""

from __future__ import annotations

import logging
import os
import re
import sys
import traceback
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Mapping

from .config import RUNTIME_DIR

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"
LOG_FILENAME = "storyflow.log"
MAX_MESSAGE_CHARS = 2000
MAX_TRACEBACK_CHARS = 6000
_MARK = "_storyflow_handler"
LEVELS = ("debug", "info", "warning", "error")

_AUTH_HEADER = re.compile(r"(?i)\b(authorization|proxy-authorization)(\s*[:=]\s*)(?:bearer\s+|basic\s+)?[^\s,;'\"]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{6,}")
_SK_KEY = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")
_KV_SECRET = re.compile(r"(?i)\b((?:api[_-]?key|apikey|password|passwd|secret|token|access[_-]?token)\s*[=:]\s*)"
                        r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&\"']+)")
_WIN_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|\\\\[\w.$-]+\\)[^\s'\"<>|,;)]*")
_UNIX_ROOTS = ("home", "Users", "tmp", "var", "etc", "usr", "mnt", "root", "opt", "private", "srv", "Volumes",
               "proc", "dev", "run", "media", "snap", "app", "workspace")
_UNIX_PATH = re.compile(r"(?<![\w<:/.~-])/(?:" + "|".join(_UNIX_ROOTS) + r")(?:/[^\s'\"<>|,;)]*)?(?![\w])")
_BLOB = re.compile(r"[A-Za-z0-9+/_=-]{40,}")
_ACCESS_QUERY = re.compile(r"((?:^|\s)\"?[A-Z]{3,7} /[^\s?\"]*)\?[^\s\"]*")


def _mask_blob(m: re.Match) -> str:
    s = m.group(0)
    # Only mixed-case + digit runs look like base64/tokens; long lowercase ids/paths pass through.
    if any(c.isupper() for c in s) and any(c.islower() for c in s) and any(c.isdigit() for c in s):
        return "<blob>"
    return s


def redact(text: str, *, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Mask secrets and absolute paths, then truncate. Pure and idempotent."""
    out = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    out = _BEARER.sub("Bearer ***", out)
    out = _SK_KEY.sub("sk-***", out)
    out = _KV_SECRET.sub(lambda m: f"{m.group(1)}***", out)
    out = _WIN_PATH.sub("<path>", out)
    out = _UNIX_PATH.sub("<path>", out)
    out = _BLOB.sub(_mask_blob, out)
    if len(out) > limit:
        out = out[:limit] + "...[truncated]"
    return out


class RedactingFilter(logging.Filter):
    """Renders the record message once, redacts + truncates it, and pre-renders/redacts any traceback."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):  # a bad format call must not raise from inside logging
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)).rstrip(),
                                     limit=MAX_TRACEBACK_CHARS)
        elif record.exc_text:
            record.exc_text = redact(record.exc_text, limit=MAX_TRACEBACK_CHARS)
        record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info, limit=MAX_TRACEBACK_CHARS)
        return True


class AccessLogFilter(logging.Filter):
    """uvicorn.access: keep method + path, drop the query string (ids/keys may live in it)."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str) and "?" in args[2]:
            record.args = (*args[:2], args[2].split("?", 1)[0], *args[3:])
        if isinstance(record.msg, str) and "?" in record.msg and not record.args:
            record.msg = _ACCESS_QUERY.sub(r"\1", record.msg)
        return True


@dataclass
class LoggingHandle:
    handlers: list[logging.Handler] = field(default_factory=list)
    log_file: Path | None = None
    level: int = logging.INFO
    _previous_level: int = logging.WARNING
    _closed: bool = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        root = logging.getLogger()
        for h in self.handlers:
            root.removeHandler(h)
            h.close()
        if not any(getattr(h, _MARK, False) for h in root.handlers):
            root.setLevel(self._previous_level)
            logging.getLogger("uvicorn.access").removeFilter(_ACCESS_FILTER)


_ACCESS_FILTER = AccessLogFilter()
_BASE_LEVEL: int | None = None  # root level before the first (still active) configure_logging call


def resolve_level(level: str | int | None, environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    raw = level if level is not None else env.get("STORYFLOW_LOG_LEVEL") or "info"
    if isinstance(raw, int):
        return raw
    value = logging.getLevelName(str(raw).strip().upper())
    if not isinstance(value, int):
        raise ValueError(f"invalid log level {raw!r}; use one of {', '.join(LEVELS)}")
    return value


def resolve_log_dir(log_dir, environ: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environ is None else environ
    if log_dir:
        return Path(log_dir)
    if env.get("STORYFLOW_LOG_DIR"):
        return Path(env["STORYFLOW_LOG_DIR"])
    return RUNTIME_DIR / "logs"


def configure_logging(*, level: str | int | None = None, log_dir=None, file: bool = False, console: bool = True,
                      max_bytes: int = 5_000_000, backups: int = 5,
                      environ: Mapping[str, str] | None = None) -> LoggingHandle:
    """Install console (+ rotating file when ``file`` or ``log_dir`` is given) handlers. Idempotent.

    ``level`` falls back to ``STORYFLOW_LOG_LEVEL`` then INFO. The log directory falls back to
    ``STORYFLOW_LOG_DIR`` then ``RUNTIME_DIR/logs``. Raises ``OSError`` if the file cannot be opened and
    ``ValueError`` for an unknown level (nothing is left half-configured in either case).
    """
    numeric = resolve_level(level, environ)
    root = logging.getLogger()
    global _BASE_LEVEL
    olds = [h for h in root.handlers if getattr(h, _MARK, False)]
    if not olds or _BASE_LEVEL is None:
        _BASE_LEVEL = root.level
    for old in olds:
        root.removeHandler(old)
        old.close()
    handlers: list[logging.Handler] = []
    log_file: Path | None = None
    try:
        formatter = logging.Formatter(LOG_FORMAT)
        if console:
            ch = logging.StreamHandler(sys.stderr)
            ch.setFormatter(formatter)
            handlers.append(ch)
        if file or log_dir is not None:
            directory = resolve_log_dir(log_dir, environ)
            directory.mkdir(parents=True, exist_ok=True)
            log_file = directory / LOG_FILENAME
            fh = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
            fh.setFormatter(formatter)
            handlers.append(fh)
    except OSError:
        for h in handlers:
            h.close()
        raise
    previous = _BASE_LEVEL
    for h in handlers:
        setattr(h, _MARK, True)
        h.addFilter(AccessLogFilter())
        h.addFilter(RedactingFilter())
        h.setLevel(numeric)
        root.addHandler(h)
    root.setLevel(numeric)
    logging.getLogger("uvicorn.access").addFilter(_ACCESS_FILTER)
    return LoggingHandle(handlers=handlers, log_file=log_file, level=numeric, _previous_level=previous)
