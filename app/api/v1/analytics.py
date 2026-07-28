"""Reports and charts.

Endpoints arrive in M7. The router exists from M1 so that the URL space,
the OpenAPI tag and the generated frontend types are stable from the start.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/analytics", tags=["analytics"])
