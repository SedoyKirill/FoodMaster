"""Store catalogues, offers and price history.

Endpoints arrive in M3. The router exists from M1 so that the URL space,
the OpenAPI tag and the generated frontend types are stable from the start.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/stores", tags=["stores"])
