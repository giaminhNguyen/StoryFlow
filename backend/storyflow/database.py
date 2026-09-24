"""SQLite engine/session setup: WAL, busy timeout, foreign keys, one session per thread.

Borrowed pattern (not code) from Subtitle_supperVip: pragmas are applied on every
connection via a connect event so both app and Alembic connections get them.

The sqlite3 datetime adapter is pinned to SQLAlchemy's own format ("%Y-%m-%d %H:%M:%S.%f")
so raw DBAPI statements (queue.py's BEGIN IMMEDIATE transactions) compare datetimes
byte-for-byte with values written through the ORM. Python 3.12+ falls back to an ISO-8601
'T' separator, which would silently mismatch every date comparison on raw connections.
"""

import sqlite3
from datetime import datetime as _datetime
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

sqlite3.register_adapter(_datetime, lambda value: value.strftime("%Y-%m-%d %H:%M:%S.%f"))


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def make_engine(url: str | None = None) -> Engine:
    url = url or settings.database_url
    if _is_sqlite(url) and not url == "sqlite://":
        path = url.split("sqlite:///", 1)[-1]
        if path and path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False} if _is_sqlite(url) else {},
    )
    if _is_sqlite(url):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute(f"PRAGMA busy_timeout={settings.busy_timeout_ms}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


engine = make_engine()

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass