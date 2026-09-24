"""Phase 5 application services: the ONLY mutation boundary for API/UI.

Callers never write ORM rows or enqueue PipelineJobs themselves. Every command here is

* a short transaction (never held across the orchestrator, the dispatcher or a runner),
* a compare-and-set on the current status (``UPDATE ... WHERE status=expected``, rowcount
  checked), so concurrent/repeated commands converge deterministically: exactly one caller
  gets ``changed=True``, the others get an idempotent no-op or a stable ``InvalidState``,
* durable: nothing about lifecycle lives in memory,
* explicit about failure: only ``storyflow.errors`` types escape for expected problems, each
  with a stable ``details["reason"]`` and never a claim token or filesystem path.

Lifecycle (ChannelWorkflow.status)::

    draft --start--> active <--pause/resume--> paused
    draft|active|paused --cancel--> cancelled          (finished/cancelled/abandoned are terminal)
    paused(step_failed) --retry--> active              (resume is refused: failed steps need retry)

``start`` never dispatches: the runtime/orchestrator does that on its next tick.
"""

import functools
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import IntegrityError

from . import queue
from .errors import Conflict, InvalidState, NotFound, NotRetryable, ValidationFailed
from .models import (
    AudioGeneration,
    CanonAnalysis,
    ChannelWorkflow,
    ChannelWorkflowStatus,
    DomainStatus,
    PauseReason,
    PipelineJob,
    RunnerInstance,
    RunnerState,
    SourceSnapshot,
    StoryGeneration,
    StoryProject,
    StoryVersion,
    TERMINAL_WORKFLOW_STATUSES,
    TTSGeneration,
    WorkflowSession,
)
from .orchestrator import Orchestrator
from .pipeline import PipelineContext
from .roles import Role

_FRESH = {"execution_options": {"populate_existing": True}}
_S = ChannelWorkflowStatus
_VALID_POLICIES = ("pause_auto_resume", "require_attention")
_OPEN_DOMAIN = (DomainStatus.QUEUED.value, DomainStatus.PROCESSING.value)
_ADD_PROJECT_OK = (_S.DRAFT.value, _S.ACTIVE.value, _S.PAUSED.value)
logger = logging.getLogger(__name__)


def _logged_command(action: str):
    """Decorator (logging only): one INFO ``key=value`` line per successful workflow command outcome."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(self, *args, **kwargs):
            out = fn(self, *args, **kwargs)
            logger.info("workflow_command workflow=%s action=%s changed=%s status=%s", out.workflow_id, action,
                        out.changed, out.status)
            return out
        return inner
    return wrap


def _logged_runner(action: str):
    """Decorator (logging only): one INFO line per successful runner command outcome."""
    def wrap(fn):
        @functools.wraps(fn)
        def inner(self, *args, **kwargs):
            out = fn(self, *args, **kwargs)
            logger.info("runner_command runner=%s action=%s changed=%s state=%s enabled=%s session=%s",
                        out.runner_id, action, out.changed, out.state, out.enabled, out.workflow_session_id or "-")
            return out
        return inner
    return wrap


_MAX_SLUG_TRIES = 50
_RETRY_CLAIM_TTL = timedelta(seconds=60)
_IN_CHUNK = 400


@dataclass(frozen=True)
class CommandResult:
    workflow_id: str
    status: str
    changed: bool                      # False = idempotent no-op
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RunnerCommandResult:
    runner_id: str
    changed: bool
    workflow_session_id: str | None
    enabled: bool
    state: str
    detail: dict = field(default_factory=dict)


def slugify(title: str) -> str:
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:110].strip("-")
    return text or "project"


def _chunks(items, size=_IN_CHUNK):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _is_unique_violation(exc: IntegrityError, *needles: str) -> bool:
    msg = str(getattr(exc, "orig", exc))
    return "UNIQUE constraint failed" in msg and any(n in msg for n in needles)


class WorkflowService:
    def __init__(self, ctx: PipelineContext, orchestrator: Orchestrator):
        self.ctx = ctx
        self.orchestrator = orchestrator

    # ------------------------------------------------------------------ helpers

    def _now(self) -> datetime:
        return self.ctx.clock()

    def _get(self, db, workflow_id: str) -> ChannelWorkflow:
        wf = db.scalar(select(ChannelWorkflow).where(ChannelWorkflow.id == workflow_id), **_FRESH)
        if wf is None:
            raise NotFound("workflow not found", reason="workflow_not_found", workflow_id=workflow_id)
        return wf

    def _cas(self, db, workflow_id: str, expect: tuple, values: dict, *extra) -> bool:
        """Guarded status transition; commits on success, rolls back otherwise."""
        res = db.execute(
            update(ChannelWorkflow)
            .where(ChannelWorkflow.id == workflow_id, ChannelWorkflow.status.in_(expect), *extra)
            .values(updated_at=self._now(), **values)
            .execution_options(synchronize_session=False))
        if res.rowcount == 1:
            db.commit()
            return True
        db.rollback()
        return False

    @staticmethod
    def _result(wf: ChannelWorkflow, changed: bool, **detail) -> CommandResult:
        return CommandResult(workflow_id=wf.id, status=wf.status, changed=changed, detail=detail)

    def _invalid(self, wf: ChannelWorkflow, command: str, reason: str, **extra) -> InvalidState:
        return InvalidState(f"cannot {command} a workflow in status {wf.status}", reason=reason,
                            workflow_id=wf.id, status=wf.status, **extra)

    def _command(self, workflow_id: str, decide):
        """Run ``decide(db, wf)`` in a bounded read-decide-CAS loop. ``decide`` returns a
        CommandResult, raises, or returns None meaning "my CAS lost; re-read and decide again"."""
        for _ in range(6):
            db = self.ctx.session_factory()
            try:
                wf = self._get(db, workflow_id)
                out = decide(db, wf)
                if out is not None:
                    return out
            finally:
                db.close()
        raise InvalidState("workflow is changing concurrently; retry the command",
                           reason="concurrent_modification", workflow_id=workflow_id)

    # ------------------------------------------------------------------ create / add_project

    @_logged_command("create")
    def create_workflow(self, name, mode="auto", config=None, *, all_agents_unavailable_policy="pause_auto_resume",
                        role_preferences=None, client_key=None) -> CommandResult:
        if not isinstance(name, str) or not name.strip():
            raise ValidationFailed("name is required", reason="name_required")
        name = name.strip()
        if len(name) > 128:
            raise ValidationFailed("name too long", reason="name_too_long", max=128)
        if not isinstance(mode, str) or not mode.strip() or len(mode) > 32:
            raise ValidationFailed("invalid mode", reason="invalid_mode")
        if config is None:
            config = {}
        if not isinstance(config, dict):
            raise ValidationFailed("config must be an object", reason="config_not_object")
        try:
            json.dumps(config)
        except (TypeError, ValueError):
            raise ValidationFailed("config must be JSON-serializable", reason="config_not_json") from None
        if all_agents_unavailable_policy not in _VALID_POLICIES:
            raise ValidationFailed("invalid all_agents_unavailable_policy", reason="invalid_policy",
                                   allowed=list(_VALID_POLICIES))
        role_preferences = self._validate_role_preferences(role_preferences)
        if client_key is not None and (not isinstance(client_key, str) or not client_key.strip()
                                       or len(client_key) > 128):
            raise ValidationFailed("invalid client_key", reason="invalid_client_key")

        if client_key is not None:
            existing = self._by_client_key(client_key)
            if existing is not None:
                return existing
        db = self.ctx.session_factory()
        try:
            now = self._now()
            session = WorkflowSession(mode=mode, status="active", role_preferences=role_preferences,
                                      all_agents_unavailable_policy=all_agents_unavailable_policy,
                                      started_at=now, created_at=now)
            db.add(session)
            db.flush()
            wf = ChannelWorkflow(workflow_session_id=session.id, name=name, mode=mode, config=config,
                                 status=_S.DRAFT.value, client_key=client_key, created_at=now, updated_at=now)
            db.add(wf)
            try:
                db.commit()  # session + workflow atomically
            except IntegrityError as exc:
                db.rollback()  # never leave a session row behind for a lost race
                if client_key is None or not _is_unique_violation(exc, "client_key"):
                    raise
                existing = self._by_client_key(client_key)
                if existing is None:
                    raise
                return existing
            return self._result(wf, True, workflow_session_id=session.id)
        finally:
            db.close()

    @staticmethod
    def _validate_role_preferences(prefs):
        if prefs is None:
            return {}
        if not isinstance(prefs, dict):
            raise ValidationFailed("role_preferences must be an object", reason="invalid_role_preferences")
        valid = {r.value for r in Role}
        for role, types in prefs.items():
            if role not in valid or not isinstance(types, list) or not all(isinstance(t, str) for t in types):
                raise ValidationFailed("invalid role_preferences", reason="invalid_role_preferences")
        return {k: list(v) for k, v in prefs.items()}

    def _by_client_key(self, client_key: str) -> CommandResult | None:
        db = self.ctx.session_factory()
        try:
            wf = db.scalar(select(ChannelWorkflow).where(ChannelWorkflow.client_key == client_key), **_FRESH)
            if wf is None:
                return None
            return self._result(wf, False, workflow_session_id=wf.workflow_session_id)
        finally:
            db.close()

    @_logged_command("add_project")
    def add_project(self, workflow_id, title, *, slug=None, description=None) -> CommandResult:
        if not isinstance(title, str) or not title.strip():
            raise ValidationFailed("title is required", reason="title_required")
        title = title.strip()
        if len(title) > 255:
            raise ValidationFailed("title too long", reason="title_too_long", max=255)
        if slug is not None:
            if not isinstance(slug, str) or not slug.strip() or len(slug) > 128:
                raise ValidationFailed("invalid slug", reason="invalid_slug")
            slug = slug.strip()
        if description is not None and not isinstance(description, str):
            raise ValidationFailed("description must be text", reason="invalid_description")

        for _ in range(_MAX_SLUG_TRIES):
            db = self.ctx.session_factory()
            try:
                now = self._now()
                # First statement of the txn: takes the write lock and proves the workflow is still
                # open, so a concurrent cancel/finish serializes before or after this insert.
                touched = db.execute(
                    update(ChannelWorkflow)
                    .where(ChannelWorkflow.id == workflow_id, ChannelWorkflow.status.in_(_ADD_PROJECT_OK))
                    .values(updated_at=now).execution_options(synchronize_session=False))
                if touched.rowcount != 1:
                    db.rollback()
                    wf = self._get(db, workflow_id)
                    raise self._invalid(wf, "add a project to", "workflow_closed")
                if slug is not None:
                    other = db.scalar(select(StoryProject).where(StoryProject.slug == slug), **_FRESH)
                    if other is not None:
                        db.rollback()
                        if other.channel_workflow_id == workflow_id:
                            return self._project_result(db, workflow_id, other, False)
                        raise Conflict("slug already used", reason="slug_taken", slug=slug)
                    chosen = slug
                else:
                    chosen = self._free_slug(db, slugify(title))
                project = StoryProject(channel_workflow_id=workflow_id, title=title, slug=chosen,
                                       description=description, created_at=now, updated_at=now)
                db.add(project)
                try:
                    db.commit()
                except IntegrityError as exc:
                    db.rollback()
                    if not _is_unique_violation(exc, "slug"):
                        raise
                    continue  # lost the slug race: re-read (given slug -> idempotent/Conflict; else next suffix)
                return self._project_result(db, workflow_id, project, True)
            finally:
                db.close()
        raise Conflict("could not allocate a unique slug", reason="slug_exhausted")

    def _project_result(self, db, workflow_id, project: StoryProject, changed: bool) -> CommandResult:
        wf = self._get(db, workflow_id)
        return self._result(wf, changed, project_id=project.id, slug=project.slug)

    @staticmethod
    def _free_slug(db, base: str) -> str:
        taken = set(db.scalars(select(StoryProject.slug).where(
            (StoryProject.slug == base) | StoryProject.slug.like(base + "-%")), **_FRESH).all())
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    # ------------------------------------------------------------------ lifecycle

    @_logged_command("start")
    def start(self, workflow_id) -> CommandResult:
        def decide(db, wf):
            if wf.status == _S.ACTIVE.value:
                return self._result(wf, False)
            if wf.status != _S.DRAFT.value:
                raise self._invalid(wf, "start", "use_resume" if wf.status == _S.PAUSED.value else "terminal")
            if wf.workflow_session_id is None:
                raise self._invalid(wf, "start", "no_session")
            has_project = exists().where(StoryProject.channel_workflow_id == wf.id)
            if self._cas(db, wf.id, (_S.DRAFT.value,), {"status": _S.ACTIVE.value, "status_reason": None,
                                                        "status_detail": None}, has_project):
                return self._result(self._get(db, wf.id), True)
            if db.scalar(select(func.count()).select_from(StoryProject)
                         .where(StoryProject.channel_workflow_id == wf.id)) == 0:
                if self._get(db, wf.id).status == _S.DRAFT.value:
                    raise self._invalid(wf, "start", "no_projects")
            return None
        return self._command(workflow_id, decide)

    @_logged_command("pause")
    def pause(self, workflow_id) -> CommandResult:
        def decide(db, wf):
            if wf.status == _S.PAUSED.value:
                return self._result(wf, False, reason=wf.status_reason)  # never clobber step_failed
            if wf.status != _S.ACTIVE.value:
                raise self._invalid(wf, "pause", "not_started" if wf.status == _S.DRAFT.value else "terminal")
            if self._cas(db, wf.id, (_S.ACTIVE.value,), {"status": _S.PAUSED.value,
                                                         "status_reason": PauseReason.OPERATOR.value,
                                                         "status_detail": None}):
                return self._result(self._get(db, wf.id), True, reason=PauseReason.OPERATOR.value)
            return None
        return self._command(workflow_id, decide)

    @_logged_command("resume")
    def resume(self, workflow_id) -> CommandResult:
        def decide(db, wf):
            if wf.status == _S.ACTIVE.value:
                return self._result(wf, False)
            if wf.status != _S.PAUSED.value:
                raise self._invalid(wf, "resume", "not_started" if wf.status == _S.DRAFT.value else "terminal")
            if wf.status_reason == PauseReason.STEP_FAILED.value:
                raise InvalidState("workflow has failed steps; use retry", reason="has_failed_steps",
                                   workflow_id=wf.id, status=wf.status,
                                   failed=self._safe_detail(wf.status_detail))
            operator_or_null = (ChannelWorkflow.status_reason.is_(None)
                                | (ChannelWorkflow.status_reason == PauseReason.OPERATOR.value))
            if self._cas(db, wf.id, (_S.PAUSED.value,), {"status": _S.ACTIVE.value, "status_reason": None,
                                                         "status_detail": None}, operator_or_null):
                return self._result(self._get(db, wf.id), True)
            return None
        return self._command(workflow_id, decide)

    @staticmethod
    def _safe_detail(detail):
        d = detail or {}
        return {k: d.get(k) for k in ("project_id", "step", "error_code") if k in d}

    # --- retry

    @_logged_command("retry")
    def retry(self, workflow_id, *, project_id=None) -> CommandResult:
        failed_detail_step = None
        db = self.ctx.session_factory()
        try:
            wf = self._get(db, workflow_id)
            if wf.status in TERMINAL_WORKFLOW_STATUSES:
                raise self._invalid(wf, "retry", "terminal")
            if wf.status != _S.PAUSED.value:
                raise NotRetryable("workflow is not paused by a failure", reason="workflow_not_paused",
                                   workflow_id=wf.id, status=wf.status)
            if wf.status_reason != PauseReason.STEP_FAILED.value:
                raise NotRetryable("workflow is paused by the operator; use resume", reason="not_failed",
                                   workflow_id=wf.id, status=wf.status)
            if project_id is not None:
                belongs = db.scalar(select(StoryProject.id).where(
                    StoryProject.id == project_id, StoryProject.channel_workflow_id == workflow_id))
                if belongs is None:
                    raise NotFound("project not in workflow", reason="project_not_in_workflow",
                                   workflow_id=workflow_id, project_id=project_id)
                # Decide retryability BEFORE claiming/mutating, so the response and the state agree.
                # A failed inline step (source) leaves no failed domain row: the durable record is the
                # workflow's status_detail, which names the project/step that paused the workflow.
                named = (wf.status_detail or {}).get("project_id") == project_id
                if not named and self.orchestrator.failed_step_of(project_id) is None:
                    raise NotRetryable("project has no failed step", reason="project_not_failed",
                                       workflow_id=workflow_id, project_id=project_id)
                failed_detail_step = (wf.status_detail or {}).get("step") if named else None
        finally:
            db.close()

        claim = self._claim_retry(workflow_id)
        if claim == "busy":
            return self._result(self._read(workflow_id), False, reason="retry_in_progress")
        if claim != "claimed":
            wf = self._read(workflow_id)  # the workflow left paused/step_failed under us
            if wf.status in TERMINAL_WORKFLOW_STATUSES:
                raise self._invalid(wf, "retry", "terminal")
            raise NotRetryable("workflow is not paused by a failure", reason="workflow_not_paused",
                               workflow_id=wf.id, status=wf.status)
        step = None
        try:
            if project_id is not None:
                step = self.orchestrator.retry_failed_step(project_id, now=self._now())
                if step is None:
                    # Inline (source) failure: re-arm by reactivating; the next tick re-runs the step.
                    self.orchestrator.resume(workflow_id, now=self._now())
                    step = failed_detail_step
            else:
                self.orchestrator.resume(workflow_id, now=self._now())
        finally:
            self._release_retry(workflow_id)
        wf = self._read(workflow_id)
        return self._result(wf, True, **({"step": step, "project_id": project_id} if project_id else {}))

    def _read(self, workflow_id) -> ChannelWorkflow:
        db = self.ctx.session_factory()
        try:
            return self._get(db, workflow_id)
        finally:
            db.close()

    def _claim_retry(self, workflow_id) -> str:
        """Serialize concurrent retries with an atomic claim marker in status_detail (BEGIN
        IMMEDIATE read-check-write). A claim older than the TTL is treated as a crashed caller."""
        now = self._now()
        db = self.ctx.session_factory()
        try:
            with queue.ImmediateTxn(db.get_bind()) as tx:
                row = tx.select_one("SELECT status, status_reason, status_detail FROM channel_workflows WHERE id=?",
                                    (workflow_id,))
                if row is None or row[0] != _S.PAUSED.value or row[1] != PauseReason.STEP_FAILED.value:
                    return "gone"
                detail = json.loads(row[2]) if isinstance(row[2], str) and row[2] else {}
                claimed_at = detail.get("retry_claimed_at")
                if claimed_at and datetime.fromisoformat(claimed_at) + _RETRY_CLAIM_TTL > now:
                    return "busy"
                detail["retry_claimed_at"] = now.isoformat()
                tx.execute("UPDATE channel_workflows SET status_detail=? WHERE id=? AND status=? AND status_reason=?",
                           (json.dumps(detail), workflow_id, _S.PAUSED.value, PauseReason.STEP_FAILED.value))
                tx.commit()
                return "claimed"
        finally:
            db.close()

    def _release_retry(self, workflow_id) -> None:
        db = self.ctx.session_factory()
        try:
            with queue.ImmediateTxn(db.get_bind()) as tx:
                row = tx.select_one("SELECT status_detail FROM channel_workflows WHERE id=? AND status=?",
                                    (workflow_id, _S.PAUSED.value))
                if row is not None and isinstance(row[0], str) and row[0]:
                    detail = json.loads(row[0])
                    if detail.pop("retry_claimed_at", None) is not None:
                        tx.execute("UPDATE channel_workflows SET status_detail=? WHERE id=? AND status=?",
                                   (json.dumps(detail), workflow_id, _S.PAUSED.value))
                tx.commit()
        finally:
            db.close()

    # --- cancel

    @_logged_command("cancel")
    def cancel(self, workflow_id) -> CommandResult:
        db = self.ctx.session_factory()
        try:
            for _ in range(6):
                wf = self._get(db, workflow_id)
                if wf.status == _S.CANCELLED.value:
                    swept = self._sweep(db, wf)  # idempotent: also cleans work a racing tick enqueued
                    return self._result(wf, False, **swept)
                if wf.status not in (_S.DRAFT.value, _S.ACTIVE.value, _S.PAUSED.value):
                    raise self._invalid(wf, "cancel", "already_finished" if wf.status == _S.FINISHED.value
                                        else "terminal")
                now = self._now()
                if self._cas(db, wf.id, (_S.DRAFT.value, _S.ACTIVE.value, _S.PAUSED.value),
                             {"status": _S.CANCELLED.value, "status_reason": None, "status_detail": None,
                              "finished_at": now}):
                    swept = self._sweep(db, self._get(db, wf.id))
                    return self._result(self._get(db, wf.id), True, **swept)
            raise InvalidState("workflow is changing concurrently; retry the command",
                               reason="concurrent_modification", workflow_id=workflow_id)
        finally:
            db.close()

    def _sweep(self, db, wf: ChannelWorkflow) -> dict:
        """After the status flip: cancel unowned jobs, then close open domain rows. Each step is its
        own short guarded transaction; completed rows/versions/chunks are never touched."""
        now = self._now()
        pids = list(db.scalars(select(StoryProject.id).where(StoryProject.channel_workflow_id == wf.id),
                               **_FRESH).all())
        snaps = self._ids(db, SourceSnapshot.id, SourceSnapshot.story_project_id, pids)
        canons = self._ids(db, CanonAnalysis.id, CanonAnalysis.source_snapshot_id, snaps)
        gens = self._ids(db, StoryGeneration.id, StoryGeneration.story_project_id, pids)
        versions = self._ids(db, StoryVersion.id, StoryVersion.story_project_id, pids)
        ttss = self._ids(db, TTSGeneration.id, TTSGeneration.story_version_id, versions)
        audios = self._ids(db, AudioGeneration.id, AudioGeneration.tts_generation_id, ttss)

        job_ids: set[str] = set()
        for model, ids in ((CanonAnalysis, canons), (StoryGeneration, gens), (TTSGeneration, ttss)):
            for part in _chunks(ids):
                job_ids.update(j for j in db.scalars(
                    select(model.pipeline_job_id).where(model.id.in_(part), model.pipeline_job_id.is_not(None))).all())
        keys = [f"{p}:{i}" for p, ids in (("canon", canons), ("story", gens), ("tts", ttss), ("audio", audios))
                for i in ids]
        for part in _chunks(keys):
            job_ids.update(db.scalars(select(PipelineJob.id).where(PipelineJob.dedupe_key.in_(part))).all())
        db.rollback()  # release the read snapshot before the write transactions

        cancelled_jobs = queue.cancel_pending_jobs(db, sorted(job_ids), now=now) if job_ids else []

        rows = 0
        for model, ids in ((CanonAnalysis, canons), (StoryGeneration, gens), (TTSGeneration, ttss),
                           (AudioGeneration, audios)):
            for part in _chunks(ids):
                res = db.execute(update(model).where(model.id.in_(part), model.status.in_(_OPEN_DOMAIN))
                                 .values(status=DomainStatus.CANCELLED.value, updated_at=now, finished_at=now)
                                 .execution_options(synchronize_session=False))
                rows += res.rowcount
                db.commit()
        return {"jobs_cancelled": len(cancelled_jobs), "domain_rows_cancelled": rows}

    @staticmethod
    def _ids(db, id_col, fk_col, parents) -> list[str]:
        out: list[str] = []
        for part in _chunks(parents):
            out += list(db.scalars(select(id_col).where(fk_col.in_(part)), **_FRESH).all())
        return out


# ---------------------------------------------------------------------------- runners


class RunnerService:
    """Runner membership/enablement commands. A runner joins a session ONLY through
    ``assign_runner``; the dispatcher's candidate query filters on RunnerInstance.workflow_session_id,
    so isolation is enforced by data, not by convention."""

    def __init__(self, session_factory, clock):
        self.session_factory = session_factory
        self.clock = clock

    def _runner(self, db, runner_id: str) -> RunnerInstance:
        r = db.scalar(select(RunnerInstance).where(RunnerInstance.id == runner_id), **_FRESH)
        if r is None:
            raise NotFound("runner not found", reason="runner_not_found", runner_id=runner_id)
        return r

    @staticmethod
    def _out(r: RunnerInstance, changed: bool, **detail) -> RunnerCommandResult:
        return RunnerCommandResult(runner_id=r.id, changed=changed, workflow_session_id=r.workflow_session_id,
                                   enabled=bool(r.enabled), state=r.state, detail=detail)

    @_logged_runner("assign")
    def assign_runner(self, runner_id, workflow_id=None, session_id=None, *, roles=None) -> RunnerCommandResult:
        if (workflow_id is None) == (session_id is None):
            raise ValidationFailed("give exactly one of workflow_id or session_id", reason="target_required")
        role_list = None
        if roles is not None:
            valid = {r.value for r in Role}
            if not isinstance(roles, (list, tuple, set)) or not roles or any(x not in valid for x in roles):
                raise ValidationFailed("invalid roles", reason="invalid_roles", allowed=sorted(valid))
            role_list = sorted(set(roles))
        db = self.session_factory()
        try:
            if workflow_id is not None:
                wf = db.scalar(select(ChannelWorkflow).where(ChannelWorkflow.id == workflow_id), **_FRESH)
                if wf is None:
                    raise NotFound("workflow not found", reason="workflow_not_found", workflow_id=workflow_id)
                if wf.status in TERMINAL_WORKFLOW_STATUSES:
                    raise InvalidState("workflow is finished", reason="terminal", workflow_id=wf.id, status=wf.status)
                if wf.workflow_session_id is None:
                    raise InvalidState("workflow has no session", reason="no_session", workflow_id=wf.id)
                sid = wf.workflow_session_id
            else:
                sess = db.scalar(select(WorkflowSession).where(WorkflowSession.id == session_id), **_FRESH)
                if sess is None:
                    raise NotFound("session not found", reason="session_not_found", session_id=session_id)
                if sess.status != "active":
                    raise InvalidState("session is not active", reason="session_closed", session_id=sess.id)
                sid = sess.id
            self._runner(db, runner_id)
            values = {"workflow_session_id": sid}
            if role_list is not None:
                values["supported_roles"] = role_list
            # Only ever touches membership (+ roles): never state, quota, cooldown or active_count.
            res = db.execute(update(RunnerInstance)
                             .where(RunnerInstance.id == runner_id, RunnerInstance.workflow_session_id.is_(None))
                             .values(**values).execution_options(synchronize_session=False))
            db.commit()
            if res.rowcount == 1:
                return self._out(self._runner(db, runner_id), True, session_id=sid)
            r = self._runner(db, runner_id)
            if r.workflow_session_id != sid:
                raise Conflict("runner is assigned to another session", reason="assigned_to_other_session",
                               runner_id=runner_id)
            if role_list is not None and sorted(r.supported_roles or []) != role_list:
                res = db.execute(update(RunnerInstance)
                                 .where(RunnerInstance.id == runner_id, RunnerInstance.workflow_session_id == sid)
                                 .values(supported_roles=role_list).execution_options(synchronize_session=False))
                db.commit()
                return self._out(self._runner(db, runner_id), True, session_id=sid, roles_updated=True)
            return self._out(r, False, session_id=sid)
        finally:
            db.close()

    @_logged_runner("unassign")
    def unassign_runner(self, runner_id) -> RunnerCommandResult:
        db = self.session_factory()
        try:
            for _ in range(6):
                r = self._runner(db, runner_id)
                if r.workflow_session_id is None:
                    return self._out(r, False)
                if (r.active_count or 0) > 0:
                    raise Conflict("runner is running jobs", reason="runner_busy", runner_id=runner_id,
                                   active_count=r.active_count)
                res = db.execute(update(RunnerInstance)
                                 .where(RunnerInstance.id == runner_id, RunnerInstance.workflow_session_id.is_not(None),
                                        RunnerInstance.active_count == 0)
                                 .values(workflow_session_id=None).execution_options(synchronize_session=False))
                db.commit()
                if res.rowcount == 1:
                    return self._out(self._runner(db, runner_id), True)
            raise Conflict("runner is changing concurrently", reason="runner_busy", runner_id=runner_id)
        finally:
            db.close()

    @_logged_runner("set_enabled")
    def set_runner_enabled(self, runner_id, enabled: bool) -> RunnerCommandResult:
        if not isinstance(enabled, bool):
            raise ValidationFailed("enabled must be a boolean", reason="invalid_enabled")
        now = self.clock()
        db = self.session_factory()
        try:
            for _ in range(6):
                r = self._runner(db, runner_id)
                if bool(r.enabled) == enabled and not (enabled and r.state == RunnerState.DISABLED.value):
                    return self._out(r, False)
                new_state = r.state
                if not enabled:
                    # Only an idle, healthy-ish runner is parked as DISABLED; busy runners finish their
                    # job, and AUTH_ERROR/OFFLINE keep their state so enabling can never resurrect them.
                    if (r.active_count or 0) == 0 and r.state not in (RunnerState.AUTH_ERROR.value,
                                                                     RunnerState.OFFLINE.value):
                        new_state = RunnerState.DISABLED.value
                elif r.state == RunnerState.DISABLED.value:
                    if r.quota_reset_at is not None and r.quota_reset_at > now:
                        new_state = RunnerState.QUOTA_EXHAUSTED.value
                    elif r.cooldown_until is not None and r.cooldown_until > now:
                        new_state = RunnerState.COOLDOWN.value
                    else:
                        new_state = RunnerState.READY.value
                res = db.execute(update(RunnerInstance)
                                 .where(RunnerInstance.id == runner_id, RunnerInstance.enabled == r.enabled,
                                        RunnerInstance.state == r.state,
                                        RunnerInstance.active_count == r.active_count)
                                 .values(enabled=enabled, state=new_state)
                                 .execution_options(synchronize_session=False))
                db.commit()
                if res.rowcount == 1:
                    return self._out(self._runner(db, runner_id), True)
            raise Conflict("runner is changing concurrently", reason="concurrent_modification",
                           runner_id=runner_id)
        finally:
            db.close()
