"""Phase 6 error contract: one machine-readable shape for every failure.

    {"error": {"code": "<stable code>", "message": "<short text>", "details": {...}}}

Codes and HTTP statuses (stable; clients switch on ``code``):

    validation            422  bad input (body/query/path), unknown route method
    not_found             404  unknown id / route / artifact
    conflict              409  collides with durable state (slug taken, runner assigned elsewhere)
    invalid_state         409  command not allowed from the current lifecycle state
    not_retryable         409  retry requested but nothing is failed
    capacity_unavailable  503  no eligible runner / provider capacity
    internal              500  unexpected failure (generic message; the traceback is only logged)

Nothing here ever echoes request bodies, filesystem paths, tokens or tracebacks.
"""

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..errors import (
    CapacityUnavailable, Conflict, InvalidState, NotFound, NotRetryable, StoryFlowError, ValidationFailed,
)

logger = logging.getLogger("storyflow.api")

STATUS_BY_CODE = {
    ValidationFailed.code: 422,
    NotFound.code: 404,
    Conflict.code: 409,
    InvalidState.code: 409,
    NotRetryable.code: 409,
    CapacityUnavailable.code: 503,
    StoryFlowError.code: 500,  # "internal"
}


def error_body(code: str, message: str, details: dict | None = None) -> dict:
    return {"error": {"code": code, "message": message, "details": details or {}}}


def error_response(code: str, message: str, details: dict | None = None, *, status: int | None = None,
                   headers: dict | None = None) -> JSONResponse:
    return JSONResponse(error_body(code, message, details), status_code=status or STATUS_BY_CODE.get(code, 500),
                        headers=headers)


def _domain_handler(_request: Request, exc: StoryFlowError) -> JSONResponse:
    if exc.code == StoryFlowError.code:  # bare StoryFlowError == unexpected
        logger.error("unexpected domain error: %s", exc.message)
        return error_response("internal", "internal error")
    return error_response(exc.code, exc.message, exc.details)


def _request_validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    # Keep location/message/type only: pydantic's `input` would echo caller data back.
    problems = [{"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg", ""), "type": e.get("type", "")}
                for e in exc.errors()]
    return error_response("validation", "request validation failed", {"problems": problems})


def _http_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    if exc.status_code == 404:
        return error_response("not_found", "not found", status=404)
    if exc.status_code == 405:
        return error_response("validation", "method not allowed", status=405,
                              headers=getattr(exc, "headers", None))
    code = "validation" if 400 <= exc.status_code < 500 else "internal"
    return error_response(code, "request failed" if code == "validation" else "internal error",
                          status=exc.status_code)


def _unhandled_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled error in API handler", exc_info=exc)
    return error_response("internal", "internal error")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StoryFlowError, _domain_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    app.add_exception_handler(Exception, _unhandled_handler)
