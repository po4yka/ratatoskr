"""Public API metadata endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.models.responses import success_response
from app.api.models.responses.common import SystemMetaResponse, SystemMetaSuccessResponse

router = APIRouter()


@router.get("/meta", response_model=SystemMetaSuccessResponse)
async def get_backend_meta() -> dict[str, Any]:
    """Return public backend/client compatibility metadata."""
    return success_response(SystemMetaResponse())
