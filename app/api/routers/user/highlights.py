"""Highlight management endpoints for summaries."""

from typing import Any

from fastapi import APIRouter, Depends

from app.api.models.requests import CreateHighlightRequest, UpdateHighlightRequest
from app.api.models.responses import (
    HighlightDeletionResponse,
    HighlightListResponse,
    HighlightResponse,
    TypedSuccessResponse,
    success_response,
)
from app.api.routers.auth import get_current_user
from app.api.services.highlight_service import SummaryHighlightService
from app.core.logging_utils import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.get(
    "/{summary_id}/highlights",
    response_model=TypedSuccessResponse[HighlightListResponse],
)
async def list_highlights(
    summary_id: int,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """List all highlights for a summary."""
    items = await SummaryHighlightService().list_highlights(
        user_id=user["user_id"],
        summary_id=summary_id,
    )
    return success_response({"highlights": items})


@router.post(
    "/{summary_id}/highlights",
    status_code=201,
    response_model=TypedSuccessResponse[HighlightResponse],
)
async def create_highlight(
    summary_id: int,
    body: CreateHighlightRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a highlight on a summary."""
    payload = await SummaryHighlightService().create_highlight(
        user_id=user["user_id"],
        summary_id=summary_id,
        body=body,
    )
    return success_response(payload)


@router.patch(
    "/{summary_id}/highlights/{highlight_id}",
    response_model=TypedSuccessResponse[HighlightResponse],
)
async def update_highlight(
    summary_id: int,
    highlight_id: str,
    body: UpdateHighlightRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Update a highlight's color or note."""
    payload = await SummaryHighlightService().update_highlight(
        user_id=user["user_id"],
        summary_id=summary_id,
        highlight_id=highlight_id,
        body=body,
    )
    return success_response(payload)


@router.delete(
    "/{summary_id}/highlights/{highlight_id}",
    response_model=TypedSuccessResponse[HighlightDeletionResponse],
)
async def delete_highlight(
    summary_id: int,
    highlight_id: str,
    user: dict[str, Any] = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a highlight."""
    await SummaryHighlightService().delete_highlight(
        user_id=user["user_id"],
        summary_id=summary_id,
        highlight_id=highlight_id,
    )
    return success_response({"deleted": True, "id": str(highlight_id)})
