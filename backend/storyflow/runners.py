"""Runner instance state helpers. Phase 1: data model + state transitions only;
no CLI/agent detection yet (that comes with AgentRunner in a later phase)."""

from datetime import datetime

from sqlalchemy import update
from sqlalchemy.orm import Session

from .models import RunnerInstance, RunnerState


def update_runner_state(db: Session, runner: RunnerInstance, state: RunnerState, *,
                        error_code: str | None = None, error_message: str | None = None,
                        cooldown_until: datetime | None = None, quota_reset_at: datetime | None = None,
                        now: datetime | None = None) -> RunnerInstance:
    """Apply a state transition and optional diagnostic fields."""
    now = now or datetime.now().astimezone().replace(tzinfo=None)
    db.execute(
        update(RunnerInstance)
        .where(RunnerInstance.id == runner.id)
        .values(
            state=state.value,
            cooldown_until=cooldown_until,
            quota_reset_at=quota_reset_at,
            last_health_at=now,
            error_code=error_code if error_code is not None else None,
            error_message=error_message if error_message is not None else None,
        )
    )
    db.commit()
    return runner


def can_claim(runner: RunnerInstance | None) -> bool:
    """Whether a runner may claim jobs right now (advisory; the queue re-checks atomically)."""
    if runner is None:
        return True
    return bool(runner.enabled) and runner.state in (RunnerState.READY, RunnerState.BUSY)