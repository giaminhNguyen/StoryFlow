"""Gateway abstraction for running tasks on an external agent platform (AionUI).

Phase 3. A gateway wraps an async task-runner platform: it detects runners, checks
health, submits a TaskPacket, polls/wait for its result, cancels, and classifies
failures. It speaks Phase 2 protocol types directly -- TaskPacket in, RunnerResult out,
ResultCode for every outcome -- so a future adapter can sit under AgentRunner without
touching the dispatcher.

AionUI integration audit (Phase 3)
==================================
AionUi exposes a stable agent-facing CLI (`aioncore`, JSON envelope
`{success, data|error, meta}` with documented error codes). Verified surface
(`aioncore capabilities` / `config capabilities` / `diagnose capabilities` /
`conversation capabilities`, schema_version 1):

  detect runners   `config agents list`        (enumerate agent backends)
  health           `diagnose health`           (backend health)
  start task       `conversation create`       (new conversation = clean assistant
                                                instance; returns conversation id)
  poll/wait        `diagnose conversations get/messages` (conversation state, read-only
                                                diagnostics)
  cancel           (none)
  error class      envelope `error.code`, e.g. runtime_auth_failed,
                   transport_unavailable, assistant_not_found

GAP -- NO STABLE TASK-RUN API
=============================
The agent-facing surface has no synchronous `task:submit -> task_id ->
poll -> result` triple for an arbitrary runner, and no cancel/stop. `conversation
create` is async and explicitly "does not send a first message"; delivering the actual
work requires a message-send primitive that is not in the stable CLI. Result recovery
today is message/async through diagnostic reads, not a contractual control loop.
Team CLI `tool_call`/messages is team-scoped collaboration, not generic task IPC.

Consequence: TaskGateway is written against the *target* surface; FakeAionGateway
implements it deterministically for tests and demos. A real `AionUiGateway` can be
dropped in when the platform exposes the task-run triple (re-enable by wiring
`conversation create` + a send/poll contract + cancel). The dispatcher remains
unaware: it only sees AgentRunner/TaskPacket/RunnerResult.
"""

import abc
import time
from collections import deque
from dataclasses import dataclass

from .protocol import ResultCode, RunnerHealth, RunnerResult, TaskPacket


class GatewayError(Exception):
    """Transport-level failure reaching the gateway (never a task failure).

    Raised when the gateway itself is unreachable, authentication to it fails, or a
    submitted task disappears. Callers classify it via TaskGateway.classify_error.
    """


@dataclass(frozen=True)
class TaskHandle:
    """Opaque reference to a submitted task. TaskPacket content is not echoed back;
    results are delivered as RunnerResult, not by re-reading the submission."""

    task_id: str
    gateway_id: str          # which runner (AionUi agent/conversation) owns it


@dataclass(frozen=True)
class TaskStatus:
    handle: TaskHandle
    done: bool = False       # False -> queued/running, call poll again
    result: RunnerResult | None = None  # populated iff done


class TaskGateway(abc.ABC):
    """Async task-runner platform behind StoryFlow. In = TaskPacket, out = RunnerResult."""

    gateway_type = "base"

    # Poll cadence used by the shared wait() loop. Fake overrides to 0.
    poll_interval = 0.05

    @abc.abstractmethod
    def detect(self) -> list[RunnerHealth]:
        """Enumerate reachable runners and their health (for RunnerRegistry seeding)."""

    @abc.abstractmethod
    def health(self, gateway_id: str) -> RunnerHealth:
        """Health of one runner; not ok (state offline/error) rather than raising."""

    @abc.abstractmethod
    def submit(self, packet: TaskPacket) -> TaskHandle:
        """Start one task. Raises GatewayError on transport/auth failure."""

    @abc.abstractmethod
    def poll(self, handle: TaskHandle) -> TaskStatus:
        """Non-blocking status. done=True carries the final RunnerResult."""

    def wait(self, handle: TaskHandle, timeout: float) -> RunnerResult:
        """Poll until done or `timeout` seconds elapse (-> ResultCode.TIMEOUT)."""
        deadline = time.monotonic() + timeout
        while True:
            status = self.poll(handle)
            if status.done:
                return status.result
            if time.monotonic() >= deadline:
                return RunnerResult(
                    code=ResultCode.TIMEOUT,
                    error_message=f"wait({handle.task_id}) exceeded {timeout}s on {self.gateway_type}",
                )
            time.sleep(self.poll_interval)

    @abc.abstractmethod
    def cancel(self, handle: TaskHandle) -> bool:
        """Request cancellation. A later poll yields ResultCode.CANCELLED."""

    def classify_error(self, error: BaseException) -> ResultCode:
        """Map a GatewayError/transport exception to an infra ResultCode.
        Gateway failures are connectivity/ops, never business results."""
        if isinstance(error, TimeoutError):
            return ResultCode.TIMEOUT
        return ResultCode.TRANSIENT_FAILURE


def gateway_error_code_to_result(code: str | None) -> ResultCode:
    """Map an AionUi envelope error.code (lowercased keyword scan) to ResultCode.

    Contract derived from the agent-facing CLI error catalog; re-validate against the
    real gateway when it lands. Everything unmatched is TRANSIENT_FAILURE so the job
    requeues into the infra-failure budget instead of dying silently.
    """
    code = (code or "").lower()
    if any(k in code for k in ("auth", "credential", "forbidden", "denied")):
        return ResultCode.AUTH_ERROR
    if any(k in code for k in ("quota", "budget")):
        return ResultCode.QUOTA_EXHAUSTED
    if any(k in code for k in ("rate", "throttl", "too_many")):
        return ResultCode.RATE_LIMITED
    if any(k in code for k in ("timeout",)):
        return ResultCode.TIMEOUT
    if any(k in code for k in ("unavailable", "connect", "unreachable")):
        return ResultCode.TRANSIENT_FAILURE
    if any(k in code for k in ("invalid", "validation", "schema")):
        return ResultCode.TASK_FAILED
    return ResultCode.TRANSIENT_FAILURE


class FakeAionGateway(TaskGateway):
    """Deterministic gateway for tests (mirrors FakeRunner's scripted approach).

    `runners` maps gateway_id -> healthy; `results` is a deque of ResultCode/RunnerResult
    consumed in order per submit (empty -> SUCCESS). A task completes after
    `latency_polls` polls (default 0 -> first poll is final), so poll/wait semantics
    are exercised without sleeping. cancel() preempts the scripted result with CANCELLED.
    """

    gateway_type = "fake"

    def __init__(self, runners=None, *, results=None, latency_polls=0):
        self.poll_interval = 0.0
        self.runners: dict[str, bool] = runners if runners is not None else {"fake": True}
        self._pending = deque(results or [])
        # Number of not-done polls before completing; None = never completes.
        self.latency_polls = latency_polls
        self._tasks: dict[str, _FakeTask] = {}
        self.submitted: list[TaskPacket] = []
        self.cancelled: list[str] = []

    def detect(self):
        return [RunnerHealth(ok=ok, runner_type=self.gateway_type,
                             state="ready" if ok else "offline")
                for gi, ok in self.runners.items()]

    def health(self, gateway_id):
        ok = self.runners.get(gateway_id)
        if ok is None:
            return RunnerHealth(ok=False, runner_type=self.gateway_type,
                                state="offline", error_message=f"unknown runner {gateway_id}")
        return RunnerHealth(ok=ok, runner_type=self.gateway_type,
                            state="ready" if ok else "offline")

    def submit(self, packet):
        handle = TaskHandle(task_id=f"{packet.task_id or 't'}:{len(self.submitted) + 1}",
                            gateway_id=next((g for g, ok in self.runners.items() if ok), "fake"))
        self.submitted.append(packet)
        self._tasks[handle.task_id] = _FakeTask(handle, self._next_result(), self.latency_polls)
        return handle

    def poll(self, handle):
        task = self._tasks.get(handle.task_id)
        if task is None:
            raise GatewayError(f"unknown task {handle.task_id}")
        if task.cancelled:
            del self._tasks[handle.task_id]
            return TaskStatus(handle, done=True,
                              result=RunnerResult(code=ResultCode.CANCELLED,
                                                  metrics={"gateway": self.gateway_type}))
        if task.polls_left is None or task.polls_left > 0:
            if task.polls_left is not None:
                task.polls_left -= 1
            return TaskStatus(handle, done=False)
        del self._tasks[handle.task_id]
        return TaskStatus(handle, done=True, result=task.result)

    def cancel(self, handle):
        task = self._tasks.get(handle.task_id)
        if task is None:
            return False
        task.cancelled = True
        self.cancelled.append(handle.task_id)
        return True

    def classify_error(self, error):
        if isinstance(error, TimeoutError):
            return ResultCode.TIMEOUT
        if isinstance(error, GatewayError):
            return gateway_error_code_to_result(str(error))
        return ResultCode.TRANSIENT_FAILURE

    def _next_result(self):
        item = self._pending.popleft() if self._pending else ResultCode.SUCCESS
        return item if isinstance(item, RunnerResult) else RunnerResult(code=item,
                                                                        metrics={"gateway": self.gateway_type})


@dataclass
class _FakeTask:
    handle: TaskHandle
    result: RunnerResult
    polls_left: int | None
    cancelled: bool = False