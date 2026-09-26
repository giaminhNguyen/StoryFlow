"""FastAPI application factory for the local HTTP API (no authentication: loopback-only by default).

``create_app(runtime_app, run_runtime=..., cors_origins=..., allowed_hosts=...)`` builds services FROM the
``RuntimeApp`` so the API and the operational runtime share one orchestrator/registry/dispatcher. Runtime
ownership (embedded thread vs external process) is documented in ``host.py``.

Security envelope: Host header must be localhost / 127.0.0.1 / testserver / [::1] (plus explicit
``allowed_hosts``); CORS is limited to http://localhost:* and http://127.0.0.1:* (plus explicit
``cors_origins``), methods GET/POST, headers Content-Type + Idempotency-Key; every response (including
500s and rejected hosts) carries ``X-Content-Type-Options: nosniff``.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles

from ..artifacts import ArtifactStore
from ..readmodels import ReadModels
from ..runtime.app import RuntimeApp
from ..services import RunnerService, WorkflowService
from ..source_service import SourceService
from . import artifacts
from .errors import error_response, register_error_handlers
from .host import RuntimeHost
from .routes import VERSION, router

logger = logging.getLogger("storyflow.api")

DEFAULT_HOSTS = ("localhost", "127.0.0.1", "testserver", "[::1]")
LOOPBACK_ORIGIN_REGEX = r"^http://(localhost|127\.0\.0\.1)(:\d+)?$"


@dataclass
class Container:
    workflows: WorkflowService
    runners: RunnerService
    read: ReadModels
    store: ArtifactStore
    runtime_app: RuntimeApp
    host: RuntimeHost | None
    sources: SourceService | None = None


def _host_of(header_value: str) -> str:
    v = header_value.strip().lower()
    if v.startswith("["):  # bracketed IPv6 literal, optional :port
        return v[: v.find("]") + 1] if "]" in v else v
    return v.split(":", 1)[0]


class _HostGuard:
    """Rejects requests whose Host header is not an allowed loopback name (DNS-rebinding defence)."""

    def __init__(self, app, allowed: set[str]):
        self.app = app
        self.allowed = allowed

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers") or [])
            host = _host_of(headers.get(b"host", b"").decode("latin-1"))
            if host not in self.allowed:
                if scope["type"] == "http":
                    resp = error_response("validation", "invalid host header", status=400)
                    await resp(scope, receive, send)
                return
        await self.app(scope, receive, send)


MAX_BODY_BYTES = 1024 * 1024  # generous for JSON commands; oversized bodies are refused early


class _BodyLimit:
    """Refuse request bodies above ``max_bytes`` (413, error contract) before they are parsed:
    by Content-Length when declared, else by counting streamed bytes. FastAPI swallows exceptions
    raised while reading the body (-> 400), so on overflow we flag it, report a disconnect to the
    app and send our own 413 instead of whatever the app answers."""

    def __init__(self, app, max_bytes: int = MAX_BODY_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope.get("headers") or []).get(b"content-length")
        too_large = error_response("validation", "request body too large", status=413)
        if declared is not None and declared.isdigit() and int(declared) > self.max_bytes:
            await too_large(scope, receive, send)
            return
        received = 0
        exceeded = False
        answered = False

        async def _receive():
            nonlocal received, exceeded
            if exceeded:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def _send(message):
            nonlocal answered
            if exceeded:
                return  # replaced by the 413 below
            if message["type"] == "http.response.start":
                answered = True
            await send(message)

        try:
            await self.app(scope, _receive, _send)
        except Exception:
            if not exceeded:
                raise  # a real application error: let the outer error middleware handle it
        if exceeded and not answered:
            await too_large(scope, receive, send)


class _NoSniff:
    """Outermost wrapper (outside Starlette's error middleware), so even 500s get the header."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"x-content-type-options"]
                headers.append((b"x-content-type-options", b"nosniff"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"   # Vite emits content-hashed file names
INDEX_CACHE_CONTROL = "no-cache"
FRONTEND_BUILD_HINT = "cd frontend && npm ci && npm run build"


class _Assets(StaticFiles):
    """Hashed build assets: immutable cache header; any filesystem oddity (null bytes, permissions, bad
    names) is the same 404 as a missing file, so no path detail ever leaks. Traversal is already refused
    by StaticFiles (resolved path must stay inside the directory)."""

    def file_response(self, *args, **kwargs):
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = ASSET_CACHE_CONTROL
        return resp

    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except (ValueError, OSError):
            raise StarletteHTTPException(status_code=404) from None


def _mount_frontend(api: FastAPI, frontend_dir) -> bool:
    """Serve ``<dir>/index.html`` at ``/`` and ``<dir>/assets`` at ``/assets``. Returns whether it is served."""
    root = Path(frontend_dir)
    index = root / "index.html"
    if not index.is_file():
        logger.warning("frontend build not found: the API works but / returns 404; build it with `%s`",
                       FRONTEND_BUILD_HINT)
        return False

    @api.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    async def _index():
        return FileResponse(index, media_type="text/html; charset=utf-8",
                            headers={"Cache-Control": INDEX_CACHE_CONTROL})

    assets = root / "assets"
    if assets.is_dir():
        api.mount("/assets", _Assets(directory=assets), name="assets")
    return True


class _StoryFlowAPI(FastAPI):
    def build_middleware_stack(self):
        return _NoSniff(super().build_middleware_stack())


def create_app(app: RuntimeApp, *, run_runtime: bool = False, cors_origins=None, allowed_hosts=None,
               frontend_dir=None) -> FastAPI:
    host = RuntimeHost(app) if run_runtime else None

    @asynccontextmanager
    async def lifespan(api: FastAPI):
        if host is not None:
            host.start()
            logger.info("runtime host started")
        try:
            yield
        finally:
            if host is not None:
                stopped = host.stop()
                logger.info("runtime host stopped clean=%s", stopped)

    api = _StoryFlowAPI(title="StoryFlow local API", version=VERSION, lifespan=lifespan,
                        docs_url=None, redoc_url=None, openapi_url=None)
    api.state.container = Container(
        workflows=WorkflowService(app.ctx, app.orchestrator),
        runners=RunnerService(app.session_factory, app.ctx.clock),
        read=ReadModels(app.ctx, app.orchestrator.chain),
        store=app.store, runtime_app=app, host=host, sources=SourceService(app.ctx))
    register_error_handlers(api)
    api.add_middleware(CORSMiddleware, allow_origins=list(cors_origins or []),
                       allow_origin_regex=LOOPBACK_ORIGIN_REGEX, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type", "Idempotency-Key"])
    api.add_middleware(_BodyLimit)
    api.add_middleware(_HostGuard, allowed={h.lower() for h in (*DEFAULT_HOSTS, *(allowed_hosts or ()))})
    api.include_router(router, prefix="/api")
    api.include_router(artifacts.router, prefix="/api")
    api.state.frontend_served = _mount_frontend(api, frontend_dir) if frontend_dir is not None else False
    return api
