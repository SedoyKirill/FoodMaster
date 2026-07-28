"""The /api/v1 router tree.

Every module's router is registered here, including the ones that are still
empty, so the URL space is fixed from M1. Routers are never included from
main.py directly.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import (
    admin,
    analytics,
    diary,
    expenses,
    family,
    fridge,
    nutrition,
    plans,
    recipes,
    shopping,
    stores,
    system,
)
from app.core.errors import ErrorResponse

_ERROR = {"model": ErrorResponse}

api_router = APIRouter(
    prefix="/api/v1",
    # Declared once so openapi-typescript emits a single shared ErrorResponse
    # type in M6 instead of a per-endpoint copy.
    responses={
        400: _ERROR,
        404: _ERROR,
        409: _ERROR,
        422: _ERROR,
        500: _ERROR,
        503: _ERROR,
    },
)

for module in (
    system,
    family,
    nutrition,
    recipes,
    stores,
    plans,
    shopping,
    fridge,
    diary,
    expenses,
    analytics,
    admin,
):
    api_router.include_router(module.router)
