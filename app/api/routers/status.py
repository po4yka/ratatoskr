"""Unauthenticated, sanitized public status endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.models.responses import PublicStatusSuccessResponse, success_response
from app.api.services.status_service import PublicStatusService, get_public_status_service

router = APIRouter()


@router.get(
    "",
    response_model=PublicStatusSuccessResponse,
    response_model_exclude_none=True,
    summary="Get public system status",
)
async def public_status(
    request: Request,
    service: PublicStatusService = Depends(get_public_status_service),
) -> PublicStatusSuccessResponse:
    """Return aggregate component health without exposing infrastructure details."""
    data = await service.get_status(request)
    return PublicStatusSuccessResponse.model_validate(success_response(data=data))
