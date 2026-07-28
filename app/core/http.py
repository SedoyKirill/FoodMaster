"""One shared HTTP client for every outbound call.

The project convention (README, "Общие соглашения") is that every external call
— store APIs, Ollama, HuggingFace — has a timeout and never takes the process
down with it. Sharing one client keeps connection pooling and those defaults in
a single place instead of scattered `httpx.AsyncClient()` instantiations.
"""

from __future__ import annotations

import httpx

_client: httpx.AsyncClient | None = None

DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


def get_http_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "ration/0.1 (+self-hosted home app)"},
        )
    return _client


async def close_http_client() -> None:
    global _client
    if _client is not None and not _client.is_closed:
        await _client.aclose()
    _client = None
