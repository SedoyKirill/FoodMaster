"""Request context: correlation id plus a structured access log.

Written as pure ASGI rather than a BaseHTTPMiddleware subclass. BaseHTTPMiddleware
wraps each call in a task group, which adds a hop between the handler and the
error envelope and has awkward edge cases around streaming responses.
"""

from __future__ import annotations

import time
from uuid import uuid4

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = structlog.get_logger("http")

#: Polled constantly by container healthchecks and the M6 dashboard; logging
#: every hit would drown everything else.
QUIET_PATHS = frozenset({"/health"})


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, header_name: str = "x-request-id") -> None:
        self.app = app
        self.header_name = header_name

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = Headers(scope=scope).get(self.header_name) or uuid4().hex
        path: str = scope["path"]

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id, method=scope["method"], path=path
        )

        status = 500
        started = time.perf_counter()

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message).append(self.header_name, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if path not in QUIET_PATHS:
                log.info(
                    "http.access",
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                )
            structlog.contextvars.clear_contextvars()
