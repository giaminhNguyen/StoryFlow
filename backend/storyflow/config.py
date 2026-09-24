"""StoryFlow runtime settings (Phase 1)."""

from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_DIR = PROJECT_ROOT / "runtime"


@dataclass
class Settings:
    database_url: str = f"sqlite:///{(RUNTIME_DIR / 'storyflow.db').as_posix()}"
    busy_timeout_ms: int = 5000
    max_attempts: int = 5
    lease_seconds: int = 60
    claim_retries: int = 10
    default_role: str = "worker"


settings = Settings()