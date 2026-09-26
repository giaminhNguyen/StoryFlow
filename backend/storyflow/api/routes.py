"""HTTP routes (prefix ``/api`` is applied by ``create_app``).

Handlers are plain ``def`` (run in the threadpool) and only call ``WorkflowService`` / ``RunnerService`` /
``SourceService`` (mutations) or ``ReadModels`` (queries) through ``request.app.state.container``. The one exception is
``/health``, which runs two read-only statements (``SELECT 1`` and ``alembic_version``). Nothing here
touches ORM rows, the queue, the dispatcher or runners, and runners are never assigned implicitly.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from ..readmodels import to_jsonable
from .schemas import (
    AddProjectBody, AddSourcesBody, AssignRunnerBody, CreateWorkflowBody, PathId, QueryId, RetryBody,
)

VERSION = "0.6.0"
router = APIRouter()
_HEAD_CACHE: dict[str, str | None] = {}


def _c(request: Request):
    return request.app.state.container


def _wf_result(request: Request, result, response: Response | None = None, *, created_status: bool = False):
    c = _c(request)
    if response is not None and created_status:
        response.status_code = 201 if result.changed else 200
    return {"result": to_jsonable(result), "workflow": to_jsonable(c.read.get_workflow(result.workflow_id))}


# ---------------------------------------------------------------------------- health


def _script_head() -> str | None:
    if "head" not in _HEAD_CACHE:
        from alembic.script import ScriptDirectory

        from ..runtime.app import alembic_config
        _HEAD_CACHE["head"] = ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()
    return _HEAD_CACHE["head"]


def _check_db(engine) -> dict:
    """Read-only. SQLAlchemyError/OSError are the only expected failure types (unreachable/corrupt DB);
    details are never reported, only ok/revision/at_head."""
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
            revision = conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
    except (SQLAlchemyError, OSError):
        return {"ok": False, "schema_revision": None, "at_head": False}
    return {"ok": True, "schema_revision": revision, "at_head": revision == _script_head()}


@router.get("/health")
def health(request: Request):
    c = _c(request)
    db = _check_db(c.runtime_app.engine)
    runners = {"registered": 0, "assigned": 0, "ready": 0, "offline": 0}
    if db["ok"]:
        try:
            snaps = c.read.list_runners()
        except SQLAlchemyError:
            db = {**db, "ok": False}
            snaps = []
        runners = {
            "registered": len(snaps),
            "assigned": sum(1 for r in snaps if r.assigned),
            "ready": sum(1 for r in snaps if r.effective_state == "ready"),
            "offline": sum(1 for r in snaps if r.effective_state in ("offline", "disabled", "auth_error")),
        }
    host = c.host
    if host is None:
        runtime = {"mode": "external", "running": None, "iteration": None, "errors": None}
        runtime_ok = True
    else:
        runtime = {"mode": "embedded", "running": host.running, "iteration": host.iteration,
                   "errors": host.last_error_count}
        runtime_ok = host.running
    degraded = not (db["ok"] and db["at_head"] and runtime_ok)
    body = {"status": "degraded" if degraded else "ok", "db": db, "runtime": runtime, "runners": runners,
            "demo": {"video_id": c.runtime_app.demo_video_id} if c.runtime_app.demo_video_id else None,
            "version": VERSION}
    return JSONResponse(body, status_code=200 if db["ok"] else 503)


# ---------------------------------------------------------------------------- providers


@router.get("/providers")
def providers(request: Request):
    """Readiness of the real integrations (subtitle / story runner / TTS engine): state + short
    path-free message only. ``configured`` is false when the app was assembled with injected
    providers (no ProviderConfig), e.g. in tests."""
    from ..readmodels import scrub_text

    stack = _c(request).runtime_app.provider_stack
    if stack is None:
        return {"configured": False, "ready": None, "providers": []}
    items = []
    for st in stack.statuses():
        items.append({"name": st.name, "kind": st.kind, "state": st.state, "usable": st.usable,
                      "message": scrub_text(st.message), "details": to_jsonable(st.details)})
    return {"configured": True, "ready": stack.ready, "providers": items}


# ---------------------------------------------------------------------------- workflows


@router.get("/workflows")
def list_workflows(request: Request, status: Annotated[str | None, Query(max_length=32)] = None,
                   limit: Annotated[int, Query(ge=0, le=500)] = 100, offset: Annotated[int, Query(ge=0)] = 0):
    return {"workflows": to_jsonable(_c(request).read.list_workflows(status=status, limit=limit, offset=offset))}


@router.post("/workflows")
def create_workflow(request: Request, response: Response, body: CreateWorkflowBody,
                    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1,
                                                                 max_length=128)] = None):
    result = _c(request).workflows.create_workflow(
        body.name, body.mode, body.config, all_agents_unavailable_policy=body.all_agents_unavailable_policy,
        role_preferences=body.role_preferences, client_key=body.client_key or idempotency_key)
    return _wf_result(request, result, response, created_status=True)


@router.get("/workflows/{workflow_id}")
def get_workflow(request: Request, workflow_id: PathId):
    return to_jsonable(_c(request).read.get_workflow(workflow_id))


@router.post("/workflows/{workflow_id}/start")
def start_workflow(request: Request, workflow_id: PathId):
    return _wf_result(request, _c(request).workflows.start(workflow_id))


@router.post("/workflows/{workflow_id}/pause")
def pause_workflow(request: Request, workflow_id: PathId):
    return _wf_result(request, _c(request).workflows.pause(workflow_id))


@router.post("/workflows/{workflow_id}/resume")
def resume_workflow(request: Request, workflow_id: PathId):
    return _wf_result(request, _c(request).workflows.resume(workflow_id))


@router.post("/workflows/{workflow_id}/retry")
def retry_workflow(request: Request, workflow_id: PathId, body: RetryBody | None = None):
    project_id = body.project_id if body else None
    return _wf_result(request, _c(request).workflows.retry(workflow_id, project_id=project_id))


@router.post("/workflows/{workflow_id}/cancel")
def cancel_workflow(request: Request, workflow_id: PathId):
    return _wf_result(request, _c(request).workflows.cancel(workflow_id))


@router.post("/workflows/{workflow_id}/projects")
def add_project(request: Request, response: Response, workflow_id: PathId, body: AddProjectBody):
    c = _c(request)
    result = c.workflows.add_project(workflow_id, body.title, slug=body.slug, description=body.description)
    response.status_code = 201 if result.changed else 200
    return {"result": to_jsonable(result), "project": to_jsonable(c.read.get_project(result.detail["project_id"]))}


@router.post("/workflows/{workflow_id}/sources")
def add_sources(request: Request, response: Response, workflow_id: PathId, body: AddSourcesBody):
    """Expand videos / playlists / channels into projects of this workflow (each video is processed once)."""
    c = _c(request)
    result = c.sources.add_sources(workflow_id, body.sources, limit=body.limit, languages=body.languages,
                                   reprocess=body.reprocess, min_duration_seconds=body.min_duration_seconds)
    response.status_code = 201 if result.changed else 200
    return {"result": to_jsonable(result), "workflow": to_jsonable(c.read.get_workflow(workflow_id))}


@router.post("/workflows/{workflow_id}/sync")
def sync_sources(request: Request, workflow_id: PathId):
    """Re-scan the workflow's channels / playlists and add only videos not seen before."""
    c = _c(request)
    result = c.sources.sync_feeds(workflow_id)
    return {"result": to_jsonable(result), "workflow": to_jsonable(c.read.get_workflow(workflow_id))}


@router.get("/workflows/{workflow_id}/feeds")
def list_feeds(request: Request, workflow_id: PathId):
    return {"feeds": to_jsonable(_c(request).read.list_feeds(workflow_id))}


@router.get("/projects/{project_id}")
def get_project(request: Request, project_id: PathId):
    return to_jsonable(_c(request).read.get_project(project_id))


@router.post("/projects/{project_id}/retry")
def retry_project(request: Request, project_id: PathId):
    """Bring a skipped / needs_attention project back into its workflow (409 when it has not ended)."""
    c = _c(request)
    result = c.workflows.retry_project(project_id)
    return {"result": to_jsonable(result), "workflow": to_jsonable(c.read.get_workflow(result.workflow_id)),
            "project": to_jsonable(c.read.get_project(project_id))}


# ---------------------------------------------------------------------------- runners


@router.get("/runners")
def list_runners(request: Request, workflow_id: QueryId = None, session_id: QueryId = None,
                 unassigned: bool = False):
    return {"runners": to_jsonable(_c(request).read.list_runners(
        workflow_id=workflow_id, session_id=session_id, unassigned=unassigned))}


@router.get("/runners/{runner_id}")
def get_runner(request: Request, runner_id: PathId):
    return to_jsonable(_c(request).read.get_runner(runner_id))


def _runner_result(request: Request, result):
    return {"result": to_jsonable(result), "runner": to_jsonable(_c(request).read.get_runner(result.runner_id))}


@router.post("/runners/{runner_id}/assign")
def assign_runner(request: Request, runner_id: PathId, body: AssignRunnerBody):
    return _runner_result(request, _c(request).runners.assign_runner(
        runner_id, workflow_id=body.workflow_id, session_id=body.session_id, roles=body.roles))


@router.post("/runners/{runner_id}/unassign")
def unassign_runner(request: Request, runner_id: PathId):
    return _runner_result(request, _c(request).runners.unassign_runner(runner_id))


@router.post("/runners/{runner_id}/enable")
def enable_runner(request: Request, runner_id: PathId):
    return _runner_result(request, _c(request).runners.set_runner_enabled(runner_id, True))


@router.post("/runners/{runner_id}/disable")
def disable_runner(request: Request, runner_id: PathId):
    return _runner_result(request, _c(request).runners.set_runner_enabled(runner_id, False))
