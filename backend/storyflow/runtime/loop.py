"""The single scheduler loop of a StoryFlow process (Phase 5, workstream C).

One ``Runtime`` per process owns: runner supervision (optional) + driving ACTIVE workflows
through ``Orchestrator.run_round``. The orchestrator is restartable and every round is an atomic
unit of durable work (claim -> execute -> apply result -> reconcile), so a graceful stop only has
to *not start* another round: nothing stays claimed by this loop, and a new Runtime on the same
database simply continues.

Fairness / bounds
-----------------
* Candidate workflows: status ACTIVE and ``workflow_session_id`` not NULL (nothing else is touched:
  draft / paused / cancelled / finished / abandoned are skipped).
* Order: workflows this process served least recently first (in-memory round-robin counter), ties
  by ``updated_at, id`` (deterministic; the counter is not durable and need not be). Without the
  counter a permanently-blocked oldest workflow would starve the rest when more than
  ``max_workflows_per_iteration`` are active.
* At most ``max_rounds_per_workflow`` rounds per workflow per iteration; stop early when a round
  makes no durable progress or the workflow leaves ACTIVE.

Stale leases are recovered by ``Orchestrator.tick`` (every round) via ``queue.recover_stale_jobs``;
the runtime deliberately does not duplicate that call.

Error isolation: the ONE broad ``except Exception`` in this package is around a single
workflow's rounds; it logs with ``logger.exception`` and records the failure in the report.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

from sqlalchemy import select

from ..models import ChannelWorkflow, ChannelWorkflowStatus
from ..orchestrator import Orchestrator
from ..pipeline import PipelineContext
from .supervisor import RefreshReport, RunnerSupervisor, bounded

logger = logging.getLogger(__name__)


@dataclass
class WorkflowReport:
    workflow_id: str
    status: str = ""
    rounds: int = 0
    progressed: bool = False
    outcomes: list[str] = field(default_factory=list)
    error: dict | None = None


@dataclass
class IterationReport:
    iteration: int = 0
    refresh: RefreshReport | None = None
    workflows: list[WorkflowReport] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)  # {workflow_id, error, message, consecutive_failures}
    stopped: bool = False

    @property
    def progressed(self) -> bool:
        return any(w.progressed for w in self.workflows)

    def summary(self) -> dict:
        """JSON-safe, payload-free (ids, statuses and counts only)."""
        return {
            "iteration": self.iteration,
            "progressed": self.progressed,
            "stopped": self.stopped,
            "refresh": self.refresh.summary() if self.refresh else None,
            "workflows": [{"id": w.workflow_id, "status": w.status, "rounds": w.rounds,
                           "progressed": w.progressed} for w in self.workflows],
            "errors": [dict(e) for e in self.errors],
        }


@dataclass
class RuntimeReport:
    iterations: int = 0
    progressed_iterations: int = 0
    errors: int = 0
    sleeps: list[float] = field(default_factory=list)
    stop_reason: str = ""  # stop_requested | max_iterations


class Runtime:
    def __init__(self, ctx: PipelineContext, orchestrator: Orchestrator, supervisor: RunnerSupervisor | None = None, *,
                 max_workflows_per_iteration: int = 10, max_rounds_per_workflow: int = 5,
                 idle_min: float = 0.05, idle_max: float = 2.0,
                 sleep: Callable[[float], object] | None = None,
                 stop_event: threading.Event | None = None):
        if max_workflows_per_iteration < 1 or max_rounds_per_workflow < 1:
            raise ValueError("max_workflows_per_iteration and max_rounds_per_workflow must be >= 1")
        if not 0 < idle_min <= idle_max:
            raise ValueError("require 0 < idle_min <= idle_max")
        self.ctx = ctx
        self.orchestrator = orchestrator
        self.supervisor = supervisor
        self.max_workflows_per_iteration = max_workflows_per_iteration
        self.max_rounds_per_workflow = max_rounds_per_workflow
        self.idle_min = idle_min
        self.idle_max = idle_max
        self._sleep = sleep
        self.stop_event = stop_event or threading.Event()
        self.iteration = 0
        self._served: dict[str, int] = {}
        self._failures: dict[str, int] = {}

    # ------------------------------------------------------------------ control

    def request_stop(self) -> None:
        """Thread- and signal-handler-safe: only sets an Event."""
        self.stop_event.set()

    # ------------------------------------------------------------------ one iteration

    def _pick(self) -> list[str]:
        db = self.ctx.session_factory()
        try:
            rows = db.execute(
                select(ChannelWorkflow.id, ChannelWorkflow.updated_at)
                .where(ChannelWorkflow.status == ChannelWorkflowStatus.ACTIVE.value,
                       ChannelWorkflow.workflow_session_id.is_not(None))
                .order_by(ChannelWorkflow.updated_at, ChannelWorkflow.id),
                execution_options={"populate_existing": True}).all()
        finally:
            db.close()
        ids = [r[0] for r in rows]
        order = {wid: i for i, wid in enumerate(ids)}
        ids.sort(key=lambda w: (self._served.get(w, -1), order[w]))
        return ids[: self.max_workflows_per_iteration]

    def run_once(self) -> IterationReport:
        self.iteration += 1
        report = IterationReport(iteration=self.iteration)
        if self.supervisor is not None:
            report.refresh = self.supervisor.refresh()
        for workflow_id in self._pick():
            if self.stop_event.is_set():
                report.stopped = True
                break
            self._served[workflow_id] = self.iteration
            report.workflows.append(self._drive(workflow_id, report))
        if self.stop_event.is_set():
            report.stopped = True
        return report

    def _drive(self, workflow_id: str, report: IterationReport) -> WorkflowReport:
        wr = WorkflowReport(workflow_id)
        try:
            for _ in range(self.max_rounds_per_workflow):
                if self.stop_event.is_set():
                    break
                rnd = self.orchestrator.run_round(workflow_id)
                wr.rounds += 1
                wr.status = rnd.workflow_status
                wr.progressed = wr.progressed or rnd.progressed
                if rnd.outcome is not None:
                    wr.outcomes.append(rnd.outcome.value)
                if not rnd.progressed or rnd.workflow_status != ChannelWorkflowStatus.ACTIVE.value:
                    break
            self._failures.pop(workflow_id, None)
        except Exception as exc:  # noqa: BLE001 - documented per-workflow isolation boundary
            count = self._failures.get(workflow_id, 0) + 1
            self._failures[workflow_id] = count
            logger.exception("workflow %s failed (consecutive failures: %d)", workflow_id, count)
            wr.error = {"workflow_id": workflow_id, "error": type(exc).__name__, "message": bounded(exc),
                        "consecutive_failures": count}
            report.errors.append(wr.error)
        return wr

    # ------------------------------------------------------------------ forever

    def _wait(self, delay: float) -> None:
        if self._sleep is not None:
            self._sleep(delay)
        else:
            self.stop_event.wait(delay)

    def run_forever(self, *, max_iterations: int | None = None,
                    on_iteration: Callable[[IterationReport], None] | None = None) -> RuntimeReport:
        out = RuntimeReport()
        delay = self.idle_min
        while True:
            if self.stop_event.is_set():
                out.stop_reason = "stop_requested"
                return out
            rep = self.run_once()
            out.iterations += 1
            out.errors += len(rep.errors)
            logger.debug("iteration n=%d workflows=%d progressed=%s errors=%d", out.iterations,
                         len(rep.workflows), rep.progressed, len(rep.errors))
            if on_iteration is not None:
                on_iteration(rep)
            if rep.progressed:
                out.progressed_iterations += 1
                delay = self.idle_min
            if self.stop_event.is_set():
                out.stop_reason = "stop_requested"
                return out
            if max_iterations is not None and out.iterations >= max_iterations:
                out.stop_reason = "max_iterations"
                return out
            if not rep.progressed:
                out.sleeps.append(delay)
                self._wait(delay)
                delay = min(delay * 2, self.idle_max)
