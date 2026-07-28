"""Matching queue, parse runs, maintenance actions.

Endpoints arrive in M3/M6. The router exists from M1 so that the URL space,
the OpenAPI tag and the generated frontend types are stable from the start.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/admin", tags=["admin"])
