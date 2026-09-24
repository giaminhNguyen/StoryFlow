"""AgentRunner abstraction + registry + deterministic FakeRunner.

No Claude/Codex/OpenCode implementation lives here (that integration is a later phase);
the dispatcher only ever talks to the AgentRunner surface, so real runners can be
swapped in without touching dispatch logic.
"""

import abc
from collections import deque

from .protocol import ResultCode, RunnerHealth, RunnerResult, TaskPacket
from .roles import DEFAULT_ROLE


class AgentRunner(abc.ABC):
    """A physical agent process the machine can talk to.

    `runner_type` is the stable identity used across roles (e.g. "claude", "codex",
    "opencode", "fake"). Role support is declared on the RunnerInstance, not here,
    keeping roles independent of runner kinds.
    """

    runner_type = "base"

    def health(self) -> RunnerHealth:
        return RunnerHealth(ok=True, runner_type=self.runner_type)

    @abc.abstractmethod
    def execute(self, packet: TaskPacket) -> RunnerResult:
        """Run one task. Must not raise for ordinary outcomes -- crashes are reported
        through exceptions, which the dispatcher classifies via classify_error()."""

    def cancel(self, task_id: str) -> bool:
        return False

    def classify_error(self, error: BaseException) -> ResultCode:
        return ResultCode.RUNNER_CRASHED


class _CrashSentinel:
    def __init__(self, exc):
        self.exc = exc

    def __repr__(self):
        return f"<raise {self.exc.__class__.__name__}>"


CRASH = _CrashSentinel(RuntimeError("simulated runner crash"))
TIMEOUT = TimeoutError("simulated runner timeout")


class FakeRunner(AgentRunner):
    """Deterministic runner for tests.

    `results` is a queue consumed in order; when empty, a result defaults to SUCCESS.
    Entries may be ResultCode, RunnerResult, or the CRASH/TIMEOUT sentinels (which make
    execute() raise so dispatcher crash-handling is exercised).
    """

    def __init__(self, runner_type="fake", *, results=None, roles=None, healthy=True):
        self.runner_type = runner_type
        self._pending = deque(results or [])
        self.roles = set(roles) if roles is not None else {DEFAULT_ROLE}
        self.healthy = healthy
        self.invocations: list[TaskPacket] = []
        self.cancelled: list[str] = []

    def health(self):
        return RunnerHealth(ok=self.healthy, runner_type=self.runner_type)

    def execute(self, packet):
        self.invocations.append(packet)
        item = self._pending.popleft() if self._pending else ResultCode.SUCCESS
        if isinstance(item, _CrashSentinel):
            raise item.exc
        if item is TIMEOUT:
            raise item
        return item if isinstance(item, RunnerResult) else RunnerResult(code=item, metrics={"invocations": len(self.invocations)})

    def cancel(self, task_id):
        self.cancelled.append(task_id)
        return True

    def classify_error(self, error):
        return ResultCode.TIMEOUT if isinstance(error, TimeoutError) else ResultCode.RUNNER_CRASHED


class RunnerRegistry:
    """Maps runner instance ids -> AgentRunner objects. Instance metadata lives in the DB;
    the registry only knows how to reach the process."""

    def __init__(self):
        self._runners: dict[str, AgentRunner] = {}

    def register(self, instance_id: str, runner: AgentRunner) -> None:
        self._runners[instance_id] = runner

    def get(self, instance_id: str) -> AgentRunner | None:
        return self._runners.get(instance_id)

    def has(self, instance_id: str) -> bool:
        return instance_id in self._runners

    def ids(self):
        return list(self._runners)

    def __len__(self):
        return len(self._runners)