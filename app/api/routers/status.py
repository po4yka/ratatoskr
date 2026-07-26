"""Unauthenticated, sanitized public status endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.models.responses import PublicStatusSuccessResponse, success_response
from app.api.services.status_service import PublicStatusService
from app.config import load_config

router = APIRouter()


def get_public_status_service() -> PublicStatusService:
    """Build the public status service from validated application configuration."""
    try:
        from app.di.api import get_current_api_runtime

        runtime = get_current_api_runtime()
    except RuntimeError:
        runtime = None
    config = runtime.cfg if runtime is not None else load_config(allow_stub_telegram=True)
    return PublicStatusService(
        deployment=config.deployment,
        llm_provider=config.runtime.llm_provider,
        database=runtime.db if runtime is not None else None,
        git_backup_enabled=config.git_backup.enabled,
        ai_backup_enabled=config.ai_backup.enabled,
        chatgpt_backup_enabled=config.ai_backup.chatgpt_enabled,
        claude_backup_enabled=config.ai_backup.claude_enabled,
    )


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
