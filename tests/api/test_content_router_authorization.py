from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.api.exceptions import ResourceNotFoundError
from app.api.routers.content import requests, summaries
from app.application.dto.request_workflow import RequestStatusDTO
from app.domain.exceptions.domain_exceptions import ResourceNotFoundError as DomainNotFound


class _SummaryUseCase:
    def __init__(self) -> None:
        self.get_summary_context_for_user = AsyncMock(return_value=None)
        self.soft_delete_summary = AsyncMock(return_value=False)
        self.toggle_favorite = AsyncMock(return_value=None)


class _RequestService:
    def __init__(self) -> None:
        self.get_request_status = AsyncMock(
            side_effect=DomainNotFound("Request", details={"request_id": 42})
        )
        self.retry_failed_request = AsyncMock(
            side_effect=DomainNotFound("Request", details={"request_id": 42})
        )


@pytest.mark.asyncio
async def test_get_summary_returns_not_found_for_non_owner() -> None:
    use_case = _SummaryUseCase()

    with pytest.raises(ResourceNotFoundError):
        await summaries.get_summary(
            summary_id=55,
            user={"user_id": 1001},
            use_case=use_case,
        )

    use_case.get_summary_context_for_user.assert_awaited_once_with(user_id=1001, summary_id=55)


@pytest.mark.asyncio
async def test_delete_summary_returns_not_found_for_non_owner() -> None:
    use_case = _SummaryUseCase()

    with pytest.raises(ResourceNotFoundError):
        await summaries.delete_summary(
            summary_id=55,
            user={"user_id": 1001},
            use_case=use_case,
        )

    use_case.soft_delete_summary.assert_awaited_once_with(user_id=1001, summary_id=55)


@pytest.mark.asyncio
async def test_request_status_maps_non_owner_to_not_found() -> None:
    request_service = _RequestService()

    with pytest.raises(ResourceNotFoundError):
        await requests.get_request_status(
            request_id=42,
            user={"user_id": 1001},
            request_service=request_service,
        )

    request_service.get_request_status.assert_awaited_once_with(1001, 42)


@pytest.mark.asyncio
async def test_request_status_router_returns_public_lifecycle_payload() -> None:
    request_service = _RequestService()
    request_service.get_request_status = AsyncMock(
        return_value=RequestStatusDTO(
            request_id=42,
            status="running",
            legacy_status="processing",
            stage="summarizing",
            progress={"percentage": 50, "value": 0.5},
            estimated_seconds_remaining=8,
            queue_position=None,
            error_details=None,
            can_retry=False,
            correlation_id="cid-status-router",
        )
    )

    response = await requests.get_request_status(
        request_id=42,
        user={"user_id": 1001},
        request_service=request_service,
    )

    assert response["success"] is True
    data = response["data"]
    assert data["status"] == "running"
    assert data["legacyStatus"] == "processing"
    assert data["stage"] == "summarizing"
    assert data["progress"] == {"percentage": 50, "value": 0.5}
    request_service.get_request_status.assert_awaited_once_with(1001, 42)
