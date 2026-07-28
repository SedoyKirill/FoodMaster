"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app import __version__
from app.api.router import api_router
from app.api.tags import OPENAPI_TAGS
from app.api.v1.system import health_router
from app.core.config import PROJECT_ROOT, get_settings
from app.core.db import dispose_engine
from app.core.errors import register_exception_handlers
from app.core.http import close_http_client
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.models import EMBEDDING_DIM
from app.core.spa import mount_spa

log = structlog.get_logger(__name__)

#: Where the M6 build lands inside the image; overridable for host-side dev.
SPA_DIST_DIR = Path("/app/web/dist") if Path("/app").is_dir() else PROJECT_ROOT / "web" / "dist"


def custom_generate_unique_id(route: APIRoute) -> str:
    """Readable, stable operationIds -> readable, stable generated TS names.

    The default is `search_recipes_api_v1_recipes_search_get`, which also
    changes whenever a URL changes. Deciding this in M1 avoids churning
    web/src/api/schema.d.ts later.
    """
    tag = route.tags[0] if route.tags else "default"
    return f"{tag}_{route.name}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    # Deliberately does NOT connect to the database. create_async_engine opens
    # no connection, so a database hiccup leaves the container up and /health
    # reporting "degraded" instead of crash-looping under restart:unless-stopped.
    if settings.embedding_dim != EMBEDDING_DIM:
        raise RuntimeError(
            f"EMBEDDING_DIM={settings.embedding_dim} does not match the schema's "
            f"VECTOR({EMBEDDING_DIM}); changing it needs a migration."
        )
    log.info(
        "app.startup",
        version=__version__,
        env=settings.app_env,
        role=settings.app_role,
        llm_provider=settings.llm_provider,
        stores=settings.stores,
    )
    try:
        yield
    finally:
        await close_http_client()
        await dispose_engine()
        log.info("app.shutdown")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_format == "json")

    app = FastAPI(
        title="Рацион — API",
        summary="Умный планировщик питания и закупок для одной семьи",
        version=__version__,
        openapi_tags=OPENAPI_TAGS,
        lifespan=lifespan,
        generate_unique_id_function=custom_generate_unique_id,
        # Otherwise every model with defaults splits into FooInput/FooOutput and
        # doubles the generated TypeScript for no benefit here.
        separate_input_output_schemas=False,
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )

    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    app.include_router(health_router)  # GET /health, no prefix
    app.include_router(api_router)  # /api/v1/*

    # MUST stay last: registers a catch-all that would shadow anything after it.
    mount_spa(app, SPA_DIST_DIR)
    return app


app = create_app()
