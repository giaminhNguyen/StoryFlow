"""Composition root for the runtime process (Phase 5, workstream C).

``build_runtime`` wires ONE engine / session factory / ArtifactStore / PipelineContext /
RunnerRegistry / Dispatcher / Orchestrator / RunnerSupervisor / Runtime. The Phase 6 API and the
application services should take these pieces from the returned ``RuntimeApp`` so a process has
exactly one orchestrator + registry and one scheduler loop.

Runner validation is decided in ONE place: ``wrap_runner_for_pipeline`` puts every runner the
supervisor registers behind ``OutputValidatingRunner`` (story + TTS validators), so invalid
artifacts become INVALID_OUTPUT inside the dispatcher path (business retry semantics).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from ..agents import AgentRunner, RunnerRegistry
from ..artifacts import ArtifactStore
from ..config import RUNTIME_DIR, settings
from ..database import make_engine
from ..dispatcher import Dispatcher
from ..models import utcnow
from ..orchestrator import Orchestrator
from ..pipeline import OutputValidatingRunner, PipelineContext
from ..protocol import ResultCode
from ..roles import Role
from ..story_steps import STORY_VALIDATORS, CanonStep, FakeStoryPipelineRunner, SourceStep, StoryStep
from ..subtitles import ExternalSubtitleClient, FakeSubtitleClient, SubtitleClient
from ..tts_steps import TTS_VALIDATORS, AudioStep, FakeAudioRunner, FakeTTSAdapterRunner, TTSStep
from .loop import Runtime
from .supervisor import RunnerProvider, RunnerSupervisor, StaticRunnerProvider

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_PIPELINE_VALIDATORS = {**STORY_VALIDATORS, **TTS_VALIDATORS}
_SCHEMA_LOCK = threading.Lock()
FAKE_CHUNKING = {"preferred_chunk_chars_max": 60, "avoid_chunk_below_chars": 10}


class SchemaError(RuntimeError):
    """The database cannot be used as-is; the message says what to do."""


def wrap_runner_for_pipeline(runner: AgentRunner, store: ArtifactStore) -> AgentRunner:
    """The single place runners get output validation. Idempotent."""
    if isinstance(runner, OutputValidatingRunner):
        return runner
    return OutputValidatingRunner(runner, store, _PIPELINE_VALIDATORS)


class PipelineRouter(AgentRunner):
    """One fake physical runner serving story_writer + tts_adapter: routes by
    ``task_config["step"]`` to the deterministic fakes. Validation is added by the supervisor's
    wrap, not here."""

    def __init__(self, store: ArtifactStore, *, runner_type: str = "fake", chunking: dict | None = None):
        self.runner_type = runner_type
        self.story = FakeStoryPipelineRunner(store)
        self.tts = FakeTTSAdapterRunner(store, chunking=chunking or FAKE_CHUNKING)
        self.audio = FakeAudioRunner(store)
        self._routes = {"canon": self.story, "story": self.story, "tts": self.tts, "audio": self.audio}

    def execute(self, packet):
        return self._routes[packet.task_config["step"]].execute(packet)

    def classify_error(self, error):
        return ResultCode.TIMEOUT if isinstance(error, TimeoutError) else ResultCode.RUNNER_CRASHED


DEMO_VIDEO_ID = "demo-video"
_DEMO_LINES = [
    "The lighthouse keeper counts the ships that never return.",
    "Every night the lamp burns a little brighter than the night before.",
    "A stranger arrives with a map that shows the sea as it was.",
    "She says the water remembers what the land forgets.",
    "By morning the keeper is gone and the lamp is still lit.",
]


def demo_subtitle_client() -> FakeSubtitleClient:
    """Deterministic offline transcript for ``--fake`` demos and the frontend smoke test."""
    track = {"language": "English", "language_code": "en", "is_generated": False, "is_translatable": True,
             "snippets": [{"text": line, "start": float(i * 3), "duration": 3.0} for i, line in enumerate(_DEMO_LINES)]}
    return FakeSubtitleClient({DEMO_VIDEO_ID: {"tracks": [track]}})


def deterministic_fake_providers(store: ArtifactStore, *, chunking: dict | None = None) -> list[RunnerProvider]:
    """Demo/test provider: one runner ``fake:fake-1`` serving story_writer + tts_adapter."""
    return [StaticRunnerProvider(
        {"fake-1": PipelineRouter(store, chunking=chunking)}, name="fake",
        roles=[Role.STORY_WRITER.value, Role.TTS_ADAPTER.value])]


# ---------------------------------------------------------------------- schema


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def ensure_schema(database_url: str) -> str:
    """Bring a MISSING/EMPTY database to alembic head; never touch a non-empty one.

    Returns "created" (migrated from empty), or "current" (already at head). A non-empty
    database at any other revision (or with tables but no alembic_version) raises SchemaError
    telling the operator to run ``alembic upgrade head`` themselves. Never destructive.
    alembic/env.py reads ``settings.database_url``, so it is swapped under a lock for the run.
    """
    engine = make_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        head = ScriptDirectory.from_config(alembic_config(database_url)).get_current_head()
        if not tables:
            action = "created"
        elif "alembic_version" not in tables:
            raise SchemaError(f"database {_redact(database_url)} has tables but no alembic_version; "
                              "refusing to modify it. Point --database-url at an empty file or migrate it manually.")
        else:
            with engine.connect() as conn:
                current = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
            if current == head:
                return "current"
            raise SchemaError(f"database is at revision {current!r}, expected {head!r}; "
                              "run `alembic upgrade head` from backend/ first.")
    finally:
        engine.dispose()
    with _SCHEMA_LOCK:
        previous = settings.database_url
        settings.database_url = database_url
        try:
            command.upgrade(alembic_config(database_url), "head")
        finally:
            settings.database_url = previous
    return action


def _redact(url: str) -> str:
    return url.split("@")[-1] if "@" in url else url


# ---------------------------------------------------------------------- app


@dataclass
class RuntimeApp:
    engine: Engine
    session_factory: Callable
    store: ArtifactStore
    ctx: PipelineContext
    registry: RunnerRegistry
    dispatcher: Dispatcher
    orchestrator: Orchestrator
    supervisor: RunnerSupervisor
    runtime: Runtime
    providers: list[RunnerProvider] = field(default_factory=list)
    demo_video_id: str | None = None   # set when the offline demo subtitle source is wired (--fake)

    def close(self) -> None:
        self.engine.dispose()


def build_runtime(*, database_url: str | None = None, artifact_root=None,
                  providers: list[RunnerProvider] | None = None,
                  subtitle_client: SubtitleClient | None = None,
                  clock: Callable[[], datetime] | None = None,
                  fake: bool = False, ensure_db_schema: bool = False,
                  **runtime_kwargs) -> RuntimeApp:
    """Wire one process. ``fake=True`` -> FakeSubtitleClient + deterministic fake providers
    (unless explicit ones are passed). ``ensure_db_schema`` runs :func:`ensure_schema` first.
    Extra kwargs go to :class:`Runtime` (idle_min, sleep, stop_event, ...)."""
    url = database_url or settings.database_url
    if ensure_db_schema:
        ensure_schema(url)
    engine = make_engine(url)
    session_factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    store = ArtifactStore(artifact_root if artifact_root is not None else RUNTIME_DIR / "artifacts")
    clock = clock or utcnow
    demo_video_id = None
    if subtitle_client is None:
        if fake:
            subtitle_client, demo_video_id = demo_subtitle_client(), DEMO_VIDEO_ID
        else:
            subtitle_client = ExternalSubtitleClient()
    if providers is None:
        providers = deterministic_fake_providers(store) if fake else []
    ctx = PipelineContext(session_factory=session_factory, store=store, subtitle_client=subtitle_client, clock=clock)
    registry = RunnerRegistry()
    dispatcher = Dispatcher(registry)
    orchestrator = Orchestrator(ctx, dispatcher, SourceStep(), [CanonStep(), StoryStep(), TTSStep(), AudioStep()])
    supervisor = RunnerSupervisor(session_factory, registry, providers, clock=clock,
                                  wrap=lambda r: wrap_runner_for_pipeline(r, store))
    runtime = Runtime(ctx, orchestrator, supervisor, **runtime_kwargs)
    return RuntimeApp(engine, session_factory, store, ctx, registry, dispatcher, orchestrator,
                      supervisor, runtime, list(providers), demo_video_id)
