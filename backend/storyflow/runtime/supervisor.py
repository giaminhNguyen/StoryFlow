"""Runner discovery + health supervision (Phase 5, workstream C).

Contract
--------
* A ``RunnerProvider`` finds runners (``detect``) and builds an in-process ``AgentRunner`` for
  one of them (``build``). It never decides roles or sessions: ``provider.roles`` is StoryFlow's
  answer to "what may runners from this provider do" and is only applied when a row is CREATED
  (an operator-edited ``supported_roles`` is never clobbered).
* Discovery NEVER assigns a workflow session. A discovered ``RunnerInstance`` has
  ``workflow_session_id = NULL`` and therefore receives zero jobs until an explicit assignment
  (application service). A re-discovered, already-assigned row keeps its session.
* Rows are identified by ``(runner_type=provider.name, external_id)``; a partial unique index
  makes concurrent refreshes from several processes converge on one row.
* Health only moves a runner between OFFLINE and its "natural" state. It never clears an active
  quota/cooldown/rate-limit window, never touches DISABLED / AUTH_ERROR, and every write is a
  guarded ``UPDATE ... WHERE state = <state we read>`` so it cannot overwrite a transition the
  dispatcher committed in between.
* A provider that fails (GatewayError / OSError / TimeoutError) is reported in
  ``RefreshReport.errors`` and logged; other providers still refresh. Rows of a failing provider
  are left unchanged (health unknown is not health bad) but their runners are still rebuilt into
  the registry from the DB so a restart during a gateway outage does not strand assigned rows.
"""

from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..agents import AgentRunner, RunnerRegistry
from ..gateway import DetectedRunner, GatewayAgentRunner, GatewayError, TaskGateway
from ..models import RunnerInstance, RunnerState, utcnow
from ..protocol import RunnerHealth

logger = logging.getLogger(__name__)

_FRESH = {"execution_options": {"populate_existing": True}}
_PROVIDER_ERRORS = (GatewayError, OSError, TimeoutError)
_MSG_LIMIT = 200

# States health may take offline. DISABLED / AUTH_ERROR / OFFLINE are never touched.
_OFFLINEABLE = (RunnerState.READY.value, RunnerState.BUSY.value, RunnerState.RATE_LIMITED.value,
                RunnerState.QUOTA_EXHAUSTED.value, RunnerState.COOLDOWN.value)


def bounded(message, limit: int = _MSG_LIMIT) -> str:
    return str(message)[:limit]


class RunnerProvider(abc.ABC):
    name: str = "provider"
    roles: list[str] = []
    max_concurrency: int = 1

    @abc.abstractmethod
    def detect(self) -> list[DetectedRunner]:
        """Runners currently reachable (id + health)."""

    @abc.abstractmethod
    def build(self, external_id: str) -> AgentRunner:
        """An AgentRunner for one runner id (must be cheap; no I/O)."""


class GatewayRunnerProvider(RunnerProvider):
    def __init__(self, gateway: TaskGateway, *, name: str | None = None, roles: list[str] | None = None,
                 max_concurrency: int = 1, timeout: float | None = None):
        self.gateway = gateway
        self.name = name or gateway.gateway_type
        self.roles = list(roles or [])
        self.max_concurrency = max_concurrency
        self.timeout = timeout

    def detect(self) -> list[DetectedRunner]:
        return self.gateway.detect()

    def build(self, external_id: str) -> AgentRunner:
        return GatewayAgentRunner(self.gateway, runner_id=external_id, timeout=self.timeout)


class StaticRunnerProvider(RunnerProvider):
    """Test/demo provider over a fixed ``external_id -> AgentRunner`` mapping.
    ``set_health`` flips health; ``fail_detect`` makes detect() raise that exception."""

    def __init__(self, runners: dict[str, AgentRunner], *, name: str = "static", roles: list[str] | None = None,
                 max_concurrency: int = 1, health: dict[str, bool] | None = None):
        self.runners = dict(runners)
        self.name = name
        self.roles = list(roles or [])
        self.max_concurrency = max_concurrency
        self.health_flags: dict[str, bool] = {k: True for k in self.runners}
        self.health_flags.update(health or {})
        self.fail_detect: BaseException | None = None

    def set_health(self, external_id: str, ok: bool) -> None:
        self.health_flags[external_id] = ok

    def detect(self) -> list[DetectedRunner]:
        if self.fail_detect is not None:
            raise self.fail_detect
        out = []
        for eid in self.runners:
            ok = self.health_flags.get(eid, True)
            out.append(DetectedRunner(eid, RunnerHealth(ok=ok, runner_type=self.name,
                                                        state="ready" if ok else "offline",
                                                        error_code=None if ok else "unhealthy")))
        return out

    def build(self, external_id: str) -> AgentRunner:
        return self.runners[external_id]


@dataclass
class RefreshReport:
    created: list[str] = field(default_factory=list)        # runner instance ids
    registered: list[str] = field(default_factory=list)     # ids newly put in the registry
    went_offline: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)        # {provider, error, message}

    def summary(self) -> dict:
        return {"created": len(self.created), "registered": len(self.registered),
                "offline": len(self.went_offline), "restored": len(self.restored),
                "errors": len(self.errors)}


def _is_external_unique(exc: IntegrityError) -> bool:
    text = str(exc.orig).lower() if getattr(exc, "orig", None) is not None else str(exc).lower()
    return ("runner_instances.external_id" in text or "uq_runner_instances_external" in text)


class RunnerSupervisor:
    def __init__(self, session_factory, registry: RunnerRegistry, providers: list[RunnerProvider], *,
                 clock: Callable[[], datetime] = utcnow,
                 wrap: Callable[[AgentRunner], AgentRunner] | None = None):
        self.session_factory = session_factory
        self.registry = registry
        self.providers = list(providers)
        self.clock = clock
        self.wrap = wrap or (lambda runner: runner)

    # ------------------------------------------------------------------ public

    def refresh(self) -> RefreshReport:
        report = RefreshReport()
        for provider in self.providers:
            try:
                self._refresh_provider(provider, report)
            except _PROVIDER_ERRORS as exc:
                self._record_error(report, provider, exc)
        return report

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _record_error(report: RefreshReport, provider: RunnerProvider, exc: BaseException) -> None:
        logger.warning("runner provider %s failed: %s: %s", provider.name, type(exc).__name__, bounded(exc))
        report.errors.append({"provider": provider.name, "error": type(exc).__name__,
                              "message": bounded(exc)})

    def _refresh_provider(self, provider: RunnerProvider, report: RefreshReport) -> None:
        try:
            detected = provider.detect()
        except _PROVIDER_ERRORS as exc:
            self._record_error(report, provider, exc)
            self._rebuild_registry(provider, report, only=None)
            return
        seen: set[str] = set()
        for det in detected:
            seen.add(det.runner_id)
            instance_id, created = self._upsert(provider, det, report)
            if created:
                report.created.append(instance_id)
                logger.info("runner_discovered runner=%s provider=%s external=%s", instance_id, provider.name,
                            det.runner_id)
            self._register(provider, det.runner_id, instance_id, report)
        # Known rows the provider did not report this time: rebuild registry, and a successful
        # detect that omits a row means the runner is not reachable -> offline.
        self._rebuild_registry(provider, report, only=seen)

    def _register(self, provider, external_id: str, instance_id: str, report: RefreshReport) -> None:
        if self.registry.has(instance_id):
            return
        try:
            runner = self.wrap(provider.build(external_id))
        except _PROVIDER_ERRORS + (KeyError, ValueError) as exc:
            self._record_error(report, provider, exc)
            return
        self.registry.register(instance_id, runner)
        report.registered.append(instance_id)

    def _rebuild_registry(self, provider, report: RefreshReport, *, only: set[str] | None) -> None:
        """Register runners for existing rows of this provider (restart recovery). When ``only``
        is a set (detect succeeded), rows outside it go offline instead."""
        db = self.session_factory()
        try:
            rows = db.execute(
                select(RunnerInstance.id, RunnerInstance.external_id)
                .where(RunnerInstance.runner_type == provider.name, RunnerInstance.external_id.is_not(None))
                .order_by(RunnerInstance.id), **_FRESH).all()
        finally:
            db.close()
        for instance_id, external_id in rows:
            if only is not None and external_id in only:
                continue
            if only is not None:
                self._apply_health(instance_id, ok=False, error_code="not_detected", message=None, report=report)
            self._register(provider, external_id, instance_id, report)

    def _upsert(self, provider: RunnerProvider, det: DetectedRunner, report: RefreshReport) -> tuple[str, bool]:
        db = self.session_factory()
        try:
            row = self._find(db, provider.name, det.runner_id)
            if row is None:
                now = self.clock()
                row = RunnerInstance(
                    workflow_session_id=None, runner_type=provider.name, external_id=det.runner_id,
                    max_concurrency=max(provider.max_concurrency, 1), supported_roles=list(provider.roles),
                    state=(RunnerState.READY if det.health.ok else RunnerState.OFFLINE).value,
                    last_health_at=now,
                    error_code=None if det.health.ok else (det.health.error_code or "unhealthy"),
                    error_message=None if det.health.ok else self._health_message(det.health),
                )
                db.add(row)
                try:
                    db.commit()
                    return row.id, True
                except IntegrityError as exc:
                    if not _is_external_unique(exc):
                        raise
                    db.rollback()
                    row = self._find(db, provider.name, det.runner_id)
                    if row is None:  # pragma: no cover - index said it exists
                        raise
            instance_id = row.id
        finally:
            db.close()
        self._apply_health(instance_id, ok=det.health.ok, error_code=det.health.error_code,
                           message=self._health_message(det.health), report=report)
        return instance_id, False

    @staticmethod
    def _health_message(health: RunnerHealth) -> str | None:
        msg = getattr(health, "error_message", None)
        return bounded(msg) if msg else None

    @staticmethod
    def _find(db, runner_type: str, external_id: str) -> RunnerInstance | None:
        return db.scalar(select(RunnerInstance).where(RunnerInstance.runner_type == runner_type,
                                                      RunnerInstance.external_id == external_id), **_FRESH)

    def _apply_health(self, instance_id: str, *, ok: bool, error_code, message, report: RefreshReport) -> None:
        now = self.clock()
        db = self.session_factory()
        try:
            row = db.scalar(select(RunnerInstance).where(RunnerInstance.id == instance_id), **_FRESH)
            if row is None:
                return
            state = row.state
            if not ok:
                if state in _OFFLINEABLE:
                    won = self._guarded(db, instance_id, state, RunnerState.OFFLINE.value, now,
                                        error_code=error_code or "unhealthy", error_message=message)
                    if won:
                        logger.info("runner_health runner=%s change=offline error_code=%s", instance_id,
                                    error_code or "unhealthy")
                        report.went_offline.append(instance_id)
                        return
                self._touch(db, instance_id, now)
                return
            if state == RunnerState.OFFLINE.value:
                if row.quota_reset_at is not None and row.quota_reset_at > now:
                    target = RunnerState.QUOTA_EXHAUSTED.value
                elif row.cooldown_until is not None and row.cooldown_until > now:
                    target = RunnerState.COOLDOWN.value
                else:
                    target = RunnerState.READY.value
                if self._guarded(db, instance_id, state, target, now, error_code=None, error_message=None):
                    logger.info("runner_health runner=%s change=restored state=%s", instance_id, target)
                    report.restored.append(instance_id)
                    return
            self._touch(db, instance_id, now)
        finally:
            db.close()

    @staticmethod
    def _guarded(db, instance_id: str, expected: str, new: str, now, *, error_code, error_message) -> bool:
        """Compare-and-set on state. quota_reset_at / cooldown_until are deliberately not in the
        VALUES: health never edits the windows."""
        res = db.execute(update(RunnerInstance)
                         .where(RunnerInstance.id == instance_id, RunnerInstance.state == expected)
                         .values(state=new, last_health_at=now, error_code=error_code,
                                 error_message=error_message))
        db.commit()
        return res.rowcount == 1

    @staticmethod
    def _touch(db, instance_id: str, now) -> None:
        db.execute(update(RunnerInstance).where(RunnerInstance.id == instance_id).values(last_health_at=now))
        db.commit()
