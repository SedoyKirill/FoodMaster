"""The unified error envelope and the SPA catch-all boundary."""

from __future__ import annotations

import httpx
from fastapi import FastAPI


async def test_unknown_api_path_returns_the_envelope_not_html(
    client: httpx.AsyncClient,
) -> None:
    """The nastiest failure mode of an SPA catch-all.

    Without the reserved-prefix guard in app/core/spa.py this would return
    index.html with HTTP 200, and the frontend would report a JSON parse error
    instead of a 404.
    """
    response = await client.get("/api/v1/definitely-not-a-route")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {"code": "NOT_FOUND", "message": "Not Found", "details": None}
    }


async def test_unknown_ui_path_serves_the_placeholder(client: httpx.AsyncClient) -> None:
    # In M1 there is no build, so every non-API path falls through to the stub.
    response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/docs" in response.text


def test_docs_and_openapi_are_not_shadowed(app: FastAPI) -> None:
    schema = app.openapi()
    # include_in_schema=False must keep the catch-all out of the contract, or
    # openapi-typescript would generate a bogus "/{spa_path}" entry in M6.
    assert "/{spa_path}" not in schema["paths"]
    assert "ErrorResponse" in schema["components"]["schemas"]


def test_operation_ids_are_unique_and_readable(app: FastAPI) -> None:
    operation_ids = [
        operation["operationId"]
        for path in app.openapi()["paths"].values()
        for operation in path.values()
        if "operationId" in operation
    ]
    assert operation_ids
    assert len(operation_ids) == len(set(operation_ids))
    # The default generator would embed the URL; ours does not.
    assert all("_api_v1_" not in operation_id for operation_id in operation_ids)
