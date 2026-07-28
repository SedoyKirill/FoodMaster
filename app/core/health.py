"""Liveness probes for /health and the M6 dashboard."""

from __future__ import annotations

import time
from typing import Literal

import httpx
import structlog

from app.core.config import get_settings
from app.core.db import check_db
from app.core.http import get_http_client

log = structlog.get_logger(__name__)

DbStatus = Literal["ok", "error"]
#: "absent" is the specification's own wording for "no Ollama here" — it covers
#: both LLM_PROVIDER=none and an unreachable server, because neither is a fault
#: from the application's point of view: everything works without an LLM.
OllamaStatus = Literal["ok", "absent"]
ModelStatus = Literal["present", "missing", "unknown"]

_PROBE_TTL_SECONDS = 15.0
_ollama_cache: tuple[float, OllamaStatus, ModelStatus] | None = None


async def probe_db(timeout: float = 2.0) -> DbStatus:
    return "ok" if await check_db(timeout) else "error"


async def probe_ollama() -> tuple[OllamaStatus, ModelStatus]:
    """Return (server status, configured-model status).

    The model half is how the first `ollama pull` — which takes ~20 minutes for
    a 5 GB model — becomes observable instead of looking like a hang.
    """
    global _ollama_cache
    settings = get_settings()

    if settings.llm_provider != "ollama":
        return "absent", "unknown"

    now = time.monotonic()
    if _ollama_cache is not None and now - _ollama_cache[0] < _PROBE_TTL_SECONDS:
        return _ollama_cache[1], _ollama_cache[2]

    status: OllamaStatus = "absent"
    model: ModelStatus = "unknown"
    try:
        response = await get_http_client().get(
            f"{settings.ollama_url.rstrip('/')}/api/tags",
            timeout=settings.ollama_probe_timeout_s,
        )
        if response.status_code == 200:
            status = "ok"
            names = {item.get("name", "") for item in response.json().get("models", [])}
            model = "present" if settings.ollama_model in names else "missing"
    except (httpx.HTTPError, ValueError):
        log.warning("health.ollama_unreachable", url=settings.ollama_url)

    _ollama_cache = (now, status, model)
    return status, model


def reset_probe_cache() -> None:
    """Test hook."""
    global _ollama_cache
    _ollama_cache = None
