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
    max_infra_attempts: int = 5
    lease_seconds: int = 60
    cooldown_seconds: int = 30
    quota_reset_seconds: int = 120
    max_failover_passes: int = 8
    claim_retries: int = 10
    default_role: str = "general_worker"


settings = Settings()