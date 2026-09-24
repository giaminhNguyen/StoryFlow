"""Phase 8 provider configuration + readiness (no secrets, nothing sent anywhere unless configured).

Real integrations stay behind the Phase 2-3 abstractions:

    subtitle  -> SubtitleClient           (``integrations/subtitle_subprocess.py``)
    story     -> AgentRunner via RunnerProvider (``integrations/claude_cli.py``)
    tts       -> AgentRunner via RunnerProvider (``integrations/vieneu.py``)

Selection is explicit: environment variables (``STORYFLOW_*``) or a local, git-ignored ``.env`` file
(simple KEY=VALUE lines; real environment variables win). Defaults never enable a real story/TTS
provider, so no text leaves the machine and no model runs unless the operator opted in.

    STORYFLOW_SUBTITLE_PROVIDER   external | fake | none            (default external)
    STORYFLOW_SUBTITLE_PYTHON     interpreter that has the upstream subtitle deps   (default: this one)
    STORYFLOW_SUBTITLE_BACKEND_DIR upstream ``backend`` dir          (default external/subtitle_suppervip/backend)
    STORYFLOW_SUBTITLE_TIMEOUT    seconds per subtitle call          (default 60)

    STORYFLOW_STORY_RUNNER        none | claude-cli | fake           (default none)
    STORYFLOW_CLAUDE_CLI          path/name of the ``claude`` executable (default: found on PATH)
    STORYFLOW_CLAUDE_MODEL        optional ``--model`` value
    STORYFLOW_STORY_TIMEOUT       seconds per story/canon task       (default 900)

    STORYFLOW_TTS_ENGINE          none | vieneu | fake               (default none)
    STORYFLOW_VIENEU_ROOT         VieNeu-TTS checkout (its own .venv is used as the interpreter)
    STORYFLOW_VIENEU_PYTHON       override interpreter               (default <root>/.venv/Scripts/python.exe | bin/python)
    STORYFLOW_VIENEU_PRECISION    fp32 | int8                        (default fp32)
    STORYFLOW_VIENEU_THREADS      CPU threads                        (default 6)
    STORYFLOW_VIENEU_VOICE        default preset voice               (default "Ngọc Huyền")
    STORYFLOW_TTS_TIMEOUT         seconds per audio run              (default 1800)

Credentials are owned by the external tools (the ``claude`` CLI's own login); StoryFlow never reads,
stores or logs them, and ``ProviderStatus`` carries only a state + a short, path-free message.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_ROOT

ENV_PREFIX = "STORYFLOW_"

SUBTITLE_PROVIDERS = ("external", "fake", "none")
STORY_RUNNERS = ("none", "claude-cli", "fake")
TTS_ENGINES = ("none", "vieneu", "fake")

# ProviderStatus.state values
READY = "ready"                    # usable now
UNAVAILABLE = "unavailable"        # configured but cannot run (executable/venv/deps missing, unhealthy)
MISCONFIGURED = "misconfigured"    # configuration value is invalid
DISABLED = "disabled"              # not selected (default for story/tts)
FAKE = "fake"                      # deterministic test double (never "production ready")


@dataclass(frozen=True)
class ProviderStatus:
    name: str          # e.g. "claude-cli"
    kind: str          # "subtitle" | "story" | "tts"
    state: str
    message: str = ""  # short, path-free, secret-free explanation
    details: dict = field(default_factory=dict)  # small JSON-safe facts (version, voice count...) - no paths/secrets

    @property
    def usable(self) -> bool:
        return self.state in (READY, FAKE)


@dataclass(frozen=True)
class ProviderConfig:
    subtitle_provider: str = "external"
    subtitle_python: str = field(default_factory=lambda: sys.executable)
    subtitle_backend_dir: str | None = None
    subtitle_timeout: float = 60.0

    story_runner: str = "none"
    claude_cli: str | None = None
    claude_model: str | None = None
    story_timeout: float = 900.0

    tts_engine: str = "none"
    vieneu_root: str | None = None
    vieneu_python: str | None = None
    vieneu_precision: str = "fp32"
    vieneu_threads: int = 6
    vieneu_voice: str = "Ngọc Huyền"
    tts_timeout: float = 1800.0

    def problems(self) -> list[str]:
        """Invalid values (reported as MISCONFIGURED; never raised so the app still starts)."""
        out = []
        if self.subtitle_provider not in SUBTITLE_PROVIDERS:
            out.append(f"STORYFLOW_SUBTITLE_PROVIDER must be one of {SUBTITLE_PROVIDERS}")
        if self.story_runner not in STORY_RUNNERS:
            out.append(f"STORYFLOW_STORY_RUNNER must be one of {STORY_RUNNERS}")
        if self.tts_engine not in TTS_ENGINES:
            out.append(f"STORYFLOW_TTS_ENGINE must be one of {TTS_ENGINES}")
        if self.vieneu_precision not in ("fp32", "int8"):
            out.append("STORYFLOW_VIENEU_PRECISION must be fp32 or int8")
        for name in ("subtitle_timeout", "story_timeout", "tts_timeout"):
            if getattr(self, name) <= 0:
                out.append(f"{name} must be > 0")
        if self.vieneu_threads < 1:
            out.append("STORYFLOW_VIENEU_THREADS must be >= 1")
        return out

    def resolved_subtitle_backend_dir(self) -> Path:
        return Path(self.subtitle_backend_dir) if self.subtitle_backend_dir else (
            PROJECT_ROOT / "external" / "subtitle_suppervip" / "backend")

    def resolved_claude_cli(self) -> str | None:
        """Configured path/name, else whatever is on PATH; None when not found."""
        candidate = self.claude_cli or "claude"
        return shutil.which(candidate)

    def resolved_vieneu_python(self) -> Path | None:
        if self.vieneu_python:
            return Path(self.vieneu_python)
        if not self.vieneu_root:
            return None
        root = Path(self.vieneu_root)
        for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
            if (root / rel).is_file():
                return root / rel
        return None


def parse_dotenv(text: str) -> dict[str, str]:
    """Tiny KEY=VALUE parser (comments, blank lines, optional quotes). No interpolation, no exports."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key:
            out[key] = value
    return out


def default_env_files() -> list[Path]:
    return [PROJECT_ROOT / "backend" / ".env", PROJECT_ROOT / ".env"]


def load_provider_config(env: dict | None = None, env_files: list[Path] | None = None) -> ProviderConfig:
    """Environment > local .env files > defaults. Only ``STORYFLOW_*`` keys are read."""
    merged: dict[str, str] = {}
    for path in (default_env_files() if env_files is None else env_files)[::-1]:
        if path.is_file():
            merged.update(parse_dotenv(path.read_text(encoding="utf-8")))
    merged.update(os.environ if env is None else env)

    def get(name, default=None):
        value = merged.get(ENV_PREFIX + name)
        return default if value is None or value == "" else value

    def number(name, default, cast):
        value = get(name)
        if value is None:
            return default
        try:
            return cast(value)
        except ValueError:
            return -1  # surfaces through problems()

    defaults = ProviderConfig()
    return ProviderConfig(
        subtitle_provider=get("SUBTITLE_PROVIDER", defaults.subtitle_provider).lower(),
        subtitle_python=get("SUBTITLE_PYTHON", defaults.subtitle_python),
        subtitle_backend_dir=get("SUBTITLE_BACKEND_DIR"),
        subtitle_timeout=number("SUBTITLE_TIMEOUT", defaults.subtitle_timeout, float),
        story_runner=get("STORY_RUNNER", defaults.story_runner).lower(),
        claude_cli=get("CLAUDE_CLI"),
        claude_model=get("CLAUDE_MODEL"),
        story_timeout=number("STORY_TIMEOUT", defaults.story_timeout, float),
        tts_engine=get("TTS_ENGINE", defaults.tts_engine).lower(),
        vieneu_root=get("VIENEU_ROOT"),
        vieneu_python=get("VIENEU_PYTHON"),
        vieneu_precision=get("VIENEU_PRECISION", defaults.vieneu_precision).lower(),
        vieneu_threads=number("VIENEU_THREADS", defaults.vieneu_threads, int),
        vieneu_voice=get("VIENEU_VOICE", defaults.vieneu_voice),
        tts_timeout=number("TTS_TIMEOUT", defaults.tts_timeout, float),
    )


# ---------------------------------------------------------------------------- stack assembly


@dataclass
class ProviderStack:
    """What ``build_runtime`` wires from a ProviderConfig: one subtitle client + zero or more runner
    providers, plus a cheap, cached ``statuses()`` for readiness reporting (API/UI/CLI)."""

    config: ProviderConfig
    subtitle_client: object
    runner_providers: list
    _status_fns: list = field(default_factory=list)

    def statuses(self) -> list[ProviderStatus]:
        return [fn() for fn in self._status_fns]

    @property
    def ready(self) -> bool:
        """Every pipeline capability (subtitle, story, tts) is usable right now."""
        by_kind = {s.kind: s for s in self.statuses()}
        return all(k in by_kind and by_kind[k].usable for k in ("subtitle", "story", "tts"))


def _static(kind_status: ProviderStatus):
    return lambda: kind_status


def build_provider_stack(config: ProviderConfig, store) -> ProviderStack:
    """Assemble providers from configuration. Nothing real is constructed unless selected; invalid
    values yield MISCONFIGURED statuses and disabled providers (the app still starts, honestly)."""
    from .runtime.app import PipelineRouter, demo_subtitle_client  # lazy: avoids an import cycle
    from .runtime.supervisor import StaticRunnerProvider
    from .roles import Role
    from .story_steps import FakeStoryPipelineRunner
    from .subtitles import ProviderUnavailable, SubtitleClient
    from .tts_steps import FakeAudioRunner, FakeTTSAdapterRunner

    problems = config.problems()
    stack = ProviderStack(config=config, subtitle_client=None, runner_providers=[])

    def bad(kind, name, text):
        return ProviderStatus(name, kind, MISCONFIGURED, text)

    class _DisabledSubtitle(SubtitleClient):
        def list_tracks(self, video_id):
            raise ProviderUnavailable("subtitle provider disabled (STORYFLOW_SUBTITLE_PROVIDER=none)")

        def fetch(self, video_id, languages=None, preference="any", allow_translation=True):
            raise ProviderUnavailable("subtitle provider disabled (STORYFLOW_SUBTITLE_PROVIDER=none)")

    # --- subtitle
    sub = config.subtitle_provider
    if any("SUBTITLE_PROVIDER" in p for p in problems):
        stack.subtitle_client = _DisabledSubtitle()
        stack._status_fns.append(_static(bad("subtitle", sub, "invalid STORYFLOW_SUBTITLE_PROVIDER")))
    elif sub == "external":
        from .integrations.subtitle_subprocess import SubprocessSubtitleClient

        client = SubprocessSubtitleClient(config)
        stack.subtitle_client = client
        stack._status_fns.append(client.status)
    elif sub == "fake":
        stack.subtitle_client = demo_subtitle_client()
        stack._status_fns.append(_static(ProviderStatus("fake", "subtitle", FAKE, "deterministic offline demo source")))
    else:
        stack.subtitle_client = _DisabledSubtitle()
        stack._status_fns.append(_static(ProviderStatus("none", "subtitle", DISABLED, "no subtitle provider selected")))

    # --- story runner
    story = config.story_runner
    if any("STORY_RUNNER" in p for p in problems):
        stack._status_fns.append(_static(bad("story", story, "invalid STORYFLOW_STORY_RUNNER")))
    elif story == "claude-cli":
        from .integrations.claude_cli import ClaudeCliProvider

        provider = ClaudeCliProvider(config, store)
        stack.runner_providers.append(provider)
        stack._status_fns.append(provider.status)
    elif story == "fake":
        stack.runner_providers.append(StaticRunnerProvider(
            {"fake-story-1": FakeStoryPipelineRunner(store)}, name="fake-story", roles=[Role.STORY_WRITER.value]))
        stack._status_fns.append(_static(ProviderStatus("fake", "story", FAKE, "deterministic fake story runner")))
    else:
        stack._status_fns.append(_static(ProviderStatus(
            "none", "story", DISABLED, "no story runner selected; set STORYFLOW_STORY_RUNNER=claude-cli")))

    # --- tts engine
    tts = config.tts_engine
    if any(("TTS_ENGINE" in p or "VIENEU" in p or "tts_timeout" in p) for p in problems):
        stack._status_fns.append(_static(bad("tts", tts, "invalid TTS configuration")))
    elif tts == "vieneu":
        from .integrations.vieneu import VieNeuProvider

        provider = VieNeuProvider(config, store)
        stack.runner_providers.append(provider)
        stack._status_fns.append(provider.status)
    elif tts == "fake":
        class _FakeTtsRouter(PipelineRouter):
            def __init__(self, store):
                super().__init__(store, runner_type="fake-tts")
                self._routes = {"tts": FakeTTSAdapterRunner(store), "audio": FakeAudioRunner(store)}

        stack.runner_providers.append(StaticRunnerProvider(
            {"fake-tts-1": _FakeTtsRouter(store)}, name="fake-tts", roles=[Role.TTS_ADAPTER.value]))
        stack._status_fns.append(_static(ProviderStatus("fake", "tts", FAKE, "deterministic fake TTS engine")))
    else:
        stack._status_fns.append(_static(ProviderStatus(
            "none", "tts", DISABLED, "no TTS engine selected; set STORYFLOW_TTS_ENGINE=vieneu")))
    return stack
