"""Recipe search and details.

Endpoints arrive in M2. The router exists from M1 so that the URL space,
the OpenAPI tag and the generated frontend types are stable from the start.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/recipes", tags=["recipes"])
