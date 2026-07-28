"""The single error format for the whole API.

Wire shape (TZ-M1 section 3, TZ-M6 section 4)::

    {"error": {"code": "NOT_FOUND", "message": "...", "details": null}}

``details`` is a documented superset of the specification: it is always present
and always ``null`` except for validation failures, where M6's forms need the
per-field errors. Without it the frontend would grow a second error path.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger(__name__)


class ErrorCode(StrEnum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"  # store API or Ollama unreachable
    LLM_UNAVAILABLE = "LLM_UNAVAILABLE"  # M5
    PRICES_STALE = "PRICES_STALE"  # M3/M5
    NO_SOLUTION = "NO_SOLUTION"  # M5 optimizer found nothing feasible


class ErrorDetail(BaseModel):
    loc: str
    message: str
    type: str


class ErrorBody(BaseModel):
    code: ErrorCode
    message: str
    details: list[ErrorDetail] | None = Field(
        default=None, description="Field-level errors; set only for VALIDATION_ERROR."
    )


class ErrorResponse(BaseModel):
    error: ErrorBody


class AppError(Exception):
    """Base class for every error the application raises deliberately."""

    status_code: int = 500
    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, *, details: list[ErrorDetail] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class BadRequestError(AppError):
    status_code = 400
    code = ErrorCode.BAD_REQUEST


class NotFoundError(AppError):
    status_code = 404
    code = ErrorCode.NOT_FOUND


class ConflictError(AppError):
    status_code = 409
    code = ErrorCode.CONFLICT


class UpstreamUnavailableError(AppError):
    status_code = 503
    code = ErrorCode.UPSTREAM_UNAVAILABLE


_STATUS_TO_CODE: dict[int, ErrorCode] = {
    400: ErrorCode.BAD_REQUEST,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.CONFLICT,
    422: ErrorCode.VALIDATION_ERROR,
    503: ErrorCode.UPSTREAM_UNAVAILABLE,
}


def error_response(
    status_code: int,
    code: ErrorCode,
    message: str,
    details: list[ErrorDetail] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details))
    return JSONResponse(
        status_code=status_code, content=body.model_dump(mode="json"), headers=headers
    )


async def _handle_app_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    log.warning("error.app", code=exc.code.value, message=exc.message)
    return error_response(exc.status_code, exc.code, exc.message, exc.details)


async def _handle_http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    code = _STATUS_TO_CODE.get(
        exc.status_code,
        ErrorCode.INTERNAL_ERROR if exc.status_code >= 500 else ErrorCode.BAD_REQUEST,
    )
    message = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return error_response(exc.status_code, code, message, headers=exc.headers)


async def _handle_validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [
        ErrorDetail(
            loc=".".join(str(part) for part in item["loc"]),
            message=str(item["msg"]),
            type=str(item["type"]),
        )
        for item in exc.errors()
    ]
    return error_response(422, ErrorCode.VALIDATION_ERROR, "Request validation failed", details)


async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    # The traceback goes to the log with the request id bound; the client gets
    # nothing that could leak internals.
    log.exception("error.unhandled", exc_type=type(exc).__name__)
    return error_response(500, ErrorCode.INTERNAL_ERROR, "Internal server error")


def register_exception_handlers(app: FastAPI) -> None:
    """Attach the envelope handlers.

    Registering Starlette's HTTPException rather than FastAPI's is deliberate:
    FastAPI's subclasses it, so one registration covers both — and, crucially,
    it also covers the 404 the router itself raises for unknown paths, which
    would otherwise come back in the default ``{"detail": ...}`` shape.
    """
    handlers: list[tuple[Any, Any]] = [
        (AppError, _handle_app_error),
        (RequestValidationError, _handle_validation_error),
        (StarletteHTTPException, _handle_http_error),
        (Exception, _handle_unexpected),
    ]
    for exception_class, handler in handlers:
        app.add_exception_handler(exception_class, handler)
