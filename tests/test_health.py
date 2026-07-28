"""The /health contract (TZ-M1 acceptance criterion)."""

from __future__ import annotations

import httpx
import pytest


@pytest.mark.db
async def test_health_returns_200_with_db_ok(
    client: httpx.AsyncClient, migrated_database: None
) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    # LLM_PROVIDER defaults to none, so Ollama is legitimately absent — and that
    # must never make the service unhealthy.
    assert body["ollama"] == "absent"
    assert body["version"]


async def test_health_echoes_a_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.headers["x-request-id"]


async def test_health_reuses_an_incoming_request_id(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"x-request-id": "abc123"})
    assert response.headers["x-request-id"] == "abc123"
