"""Structured logging.

structlog rather than stdlib+json-formatter for one concrete reason:
``structlog.contextvars``. The request id is bound once in middleware and every
downstream log line — in any module, at any depth — carries it without being
threaded through function signatures. Later the same mechanism carries
``plan_id``, ``parse_run_id`` and ``store``.
"""

from __future__ import annotations

import logging
import sys

import structlog

# Loggers whose records must flow through our formatter rather than their own.
_ADOPTED_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "sqlalchemy.engine",
    "alembic",
    "apscheduler",
    "httpx",
)


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=True)
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        # Makes records from SQLAlchemy/uvicorn/alembic come out in the same
        # shape as our own.
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    for name in _ADOPTED_LOGGERS:
        adopted = logging.getLogger(name)
        adopted.handlers = []
        adopted.propagate = True

    # We emit a richer access log ourselves; uvicorn is started with
    # --no-access-log, this is the belt to that suspenders.
    logging.getLogger("uvicorn.access").disabled = True
