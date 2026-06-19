from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from app.api.exceptions import ResourceNotFoundError
from app.api.routers.content import requests, summaries
from app.application.dto.request_workflow import RequestStatusDTO
from app.application.services.related_reads_service import RelatedReadItem
from app.application.services.request_service import RequestService
from app.application.use_cases.summary_read_model import SummaryReadModelUseCase
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


class _RelatedReadsService:
    def __init__(self) -> None:
        self.find_related = AsyncMock(
            return_value=[
                RelatedReadItem(
                    summary_id=77,
                    request_id=88,
                    title="Related item",
                    age_label="2d",
                    similarity_score=0.91,
                )
            ]
        )


@pytest.mark.asyncio
async def test_get_summary_returns_not_found_for_non_owner() -> None:
    use_case = _SummaryUseCase()

    with pytest.raises(ResourceNotFoundError):
        await summaries.get_summary(
            summary_id=55,
            user={"user_id": 1001},
            use_case=cast("SummaryReadModelUseCase", use_case),
        )

    use_case.get_summary_context_for_user.assert_awaited_once_with(user_id=1001, summary_id=55)


@pytest.mark.asyncio
async def test_get_related_reads_returns_not_found_for_non_owner() -> None:
    use_case = _SummaryUseCase()

    with pytest.raises(ResourceNotFoundError):
        await summaries.get_related_reads(
            summary_id=55,
            user={"user_id": 1001},
            use_case=cast("SummaryReadModelUseCase", use_case),
            related_reads_service=cast("summaries.RelatedReadsService", _RelatedReadsService()),
        )

    use_case.get_summary_context_for_user.assert_awaited_once_with(user_id=1001, summary_id=55)


@pytest.mark.asyncio
async def test_get_related_reads_returns_user_scoped_bundle() -> None:
    use_case = _SummaryUseCase()
    use_case.get_summary_context_for_user = AsyncMock(
        return_value={
            "summary": {
                "id": 55,
                "lang": "en",
                "json_payload": {
                    "summary_250": "A concise source summary",
                    "summary_1000": "A longer source summary",
                    "tldr": "Source tldr",
                    "topic_tags": ["ai"],
                },
            },
            "request_id": 66,
        }
    )
    related_reads_service = _RelatedReadsService()

    response = await summaries.get_related_reads(
        summary_id=55,
        user={"user_id": 1001},
        use_case=cast("SummaryReadModelUseCase", use_case),
        related_reads_service=cast("summaries.RelatedReadsService", related_reads_service),
    )

    assert response["success"] is True
    assert response["data"] == {
        "summaryId": 55,
        "related": [
            {
                "summaryId": 77,
                "requestId": 88,
                "title": "Related item",
                "ageLabel": "2d",
                "similarityScore": 0.91,
            }
        ],
        "count": 1,
    }
    use_case.get_summary_context_for_user.assert_awaited_once_with(user_id=1001, summary_id=55)
    related_reads_service.find_related.assert_awaited_once_with(
        {
            "summary_250": "A concise source summary",
            "summary_1000": "A longer source summary",
            "tldr": "Source tldr",
            "topic_tags": ["ai"],
        },
        exclude_request_id=66,
        language="en",
    )


@pytest.mark.asyncio
async def test_related_reads_vector_adapter_scopes_search_to_user() -> None:
    vector_search = SimpleNamespace(
        search=AsyncMock(
            return_value=SimpleNamespace(
                results=[
                    SimpleNamespace(
                        request_id=88,
                        summary_id=77,
                        similarity_score=0.91,
                        url="https://example.com/related",
                        title="Related item",
                        snippet="Related snippet",
                        source="summary",
                        published_at="2026-06-17",
                    )
                ]
            )
        )
    )
    adapter = summaries._RelatedReadsVectorAdapter(
        vector_search,
        user_id=1001,
        user_scope="private",
        max_results=10,
    )

    hits = await adapter.search("source summary", correlation_id="cid-related")

    vector_search.search.assert_awaited_once_with(
        "source summary",
        user_scope="private",
        user_id=1001,
        limit=10,
        correlation_id="cid-related",
    )
    assert hits[0].summary_id == 77
    assert hits[0].request_id == 88


@pytest.mark.asyncio
async def test_delete_summary_returns_not_found_for_non_owner() -> None:
    use_case = _SummaryUseCase()

    with pytest.raises(ResourceNotFoundError):
        await summaries.delete_summary(
            summary_id=55,
            user={"user_id": 1001},
            use_case=cast("SummaryReadModelUseCase", use_case),
        )

    use_case.soft_delete_summary.assert_awaited_once_with(user_id=1001, summary_id=55)


@pytest.mark.asyncio
async def test_request_status_maps_non_owner_to_not_found() -> None:
    request_service = _RequestService()

    with pytest.raises(ResourceNotFoundError):
        await requests.get_request_status(
            request_id=42,
            user={"user_id": 1001},
            request_service=cast("RequestService", request_service),
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
        request_service=cast("RequestService", request_service),
    )

    assert response["success"] is True
    data = response["data"]
    assert data["status"] == "running"
    assert data["legacyStatus"] == "processing"
    assert data["stage"] == "summarizing"
    assert data["progress"] == {"percentage": 50, "value": 0.5}
    request_service.get_request_status.assert_awaited_once_with(1001, 42)
