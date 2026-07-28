"""System status and the health probe (M1, consumed by M6's dashboard)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import __version__
from app.api.deps import SessionDep, SettingsDep
from app.core.health import DbStatus, ModelStatus, OllamaStatus, probe_db, probe_ollama
from app.core.models import ParseRun

router = APIRouter(prefix="/system", tags=["system"])

#: Mounted at the root, without the /api/v1 prefix, per TZ-M1 section 3.
health_router = APIRouter(tags=["system"])


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: DbStatus
    ollama: OllamaStatus
    ollama_model: ModelStatus = Field(
        description="Whether OLLAMA_MODEL is already pulled. Watch the first pull here."
    )
    version: str


class StoreFreshness(BaseModel):
    store: str
    last_success_at: datetime | None = None
    last_status: str | None = None


class SystemStatusResponse(BaseModel):
    """Everything M6's dashboard needs in one call."""

    version: str
    llm_provider: str
    ollama: OllamaStatus
    ollama_model: ModelStatus
    stores: list[StoreFreshness]
    matching_queue: int = Field(
        default=0, description="Products awaiting manual match confirmation (M3)."
    )


@health_router.get("/health", summary="Проверка состояния сервиса")
async def health(response: Response) -> HealthResponse:
    """503 only when the database is unreachable.

    An absent Ollama must never make the container unhealthy: the whole system
    is required to work without an LLM, and during the first model pull the
    server is up but the model is not — marking `api` unhealthy there would
    stop the scheduler from ever starting.
    """
    db_status, (ollama_status, model_status) = await asyncio.gather(probe_db(), probe_ollama())
    if db_status != "ok":
        response.status_code = 503
    return HealthResponse(
        status="ok" if db_status == "ok" else "degraded",
        db=db_status,
        ollama=ollama_status,
        ollama_model=model_status,
        version=__version__,
    )


@router.get("/status", summary="Сводное состояние системы для дашборда")
async def system_status(session: SessionDep, settings: SettingsDep) -> SystemStatusResponse:
    ollama_status, model_status = await probe_ollama()

    freshness: list[StoreFreshness] = []
    for store in settings.stores:
        last_ok = await session.scalar(
            select(ParseRun.finished_at)
            .where(ParseRun.store == store, ParseRun.status == "ok")
            .order_by(ParseRun.started_at.desc())
            .limit(1)
        )
        last_status = await session.scalar(
            select(ParseRun.status)
            .where(ParseRun.store == store)
            .order_by(ParseRun.started_at.desc())
            .limit(1)
        )
        freshness.append(
            StoreFreshness(store=store, last_success_at=last_ok, last_status=last_status)
        )

    return SystemStatusResponse(
        version=__version__,
        llm_provider=settings.llm_provider,
        ollama=ollama_status,
        ollama_model=model_status,
        stores=freshness,
        # Filled in by M3; the field exists now so the dashboard contract is stable.
        matching_queue=0,
    )
