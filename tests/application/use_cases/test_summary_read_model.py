from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.application.use_cases.summary_read_model import SummaryReadModelUseCase


class _OptimizedSummaryRepository:
    def __init__(self, context: dict[str, object] | None) -> None:
        self.get_summary_context_mock = AsyncMock(return_value=context)
        self.get_aggregation_source_bundle_mock = AsyncMock(return_value=None)
        self.async_get_summary_by_id = AsyncMock()

    async def async_get_summary_context_by_id(self, summary_id: int) -> dict[str, object] | None:
        return await self.get_summary_context_mock(summary_id)

    async def async_get_aggregation_source_bundle_for_summary(
        self, summary_id: int
    ) -> dict[str, object] | None:
        return await self.get_aggregation_source_bundle_mock(summary_id)

    async def async_get_aggregation_source_bundle_for_summary_owned_by_user(
        self, summary_id: int, user_id: int
    ) -> dict[str, object] | None:
        return await self.get_aggregation_source_bundle_mock(summary_id, user_id)


@pytest.mark.asyncio
async def test_get_summary_context_for_user_prefers_joined_repository_path() -> None:
    summary_repo = _OptimizedSummaryRepository(
        {
            "summary": {"id": 7, "request_id": 70, "user_id": 3, "is_deleted": False},
            "request": {"id": 70, "user_id": 3, "status": "ok"},
            "crawl_result": {"request": 70, "status": "ok"},
        }
    )
    request_repo = AsyncMock()
    crawl_repo = AsyncMock()
    llm_repo = AsyncMock()
    llm_repo.async_get_llm_calls_by_request.return_value = [{"id": 1, "request": 70}]

    use_case = SummaryReadModelUseCase(
        summary_repository=cast("Any", summary_repo),
        request_repository=request_repo,
        crawl_result_repository=crawl_repo,
        llm_repository=llm_repo,
    )

    context = await use_case.get_summary_context_for_user(user_id=3, summary_id=7)

    assert context == {
        "summary": {"id": 7, "request_id": 70, "user_id": 3, "is_deleted": False},
        "request": {"id": 70, "user_id": 3, "status": "ok"},
        "request_id": 70,
        "crawl_result": {"request": 70, "status": "ok"},
        "llm_calls": [{"id": 1, "request": 70}],
        "aggregation_source_bundle": None,
        "transcription_artifact": None,
    }
    summary_repo.get_summary_context_mock.assert_awaited_once_with(7)
    summary_repo.get_aggregation_source_bundle_mock.assert_awaited_once_with(7, 3)
    summary_repo.async_get_summary_by_id.assert_not_called()
    request_repo.async_get_request_by_id.assert_not_called()
    crawl_repo.async_get_crawl_result_by_request.assert_not_called()
    llm_repo.async_get_llm_calls_by_request.assert_awaited_once_with(70)


@pytest.mark.asyncio
async def test_get_summary_context_for_user_does_not_fetch_bundle_for_other_user() -> None:
    summary_repo = _OptimizedSummaryRepository(
        {
            "summary": {"id": 7, "request_id": 70, "user_id": 4, "is_deleted": False},
            "request": {"id": 70, "user_id": 4, "status": "ok"},
            "crawl_result": None,
        }
    )
    use_case = SummaryReadModelUseCase(
        summary_repository=cast("Any", summary_repo),
        request_repository=AsyncMock(),
        crawl_result_repository=AsyncMock(),
        llm_repository=AsyncMock(),
    )

    assert await use_case.get_summary_context_for_user(user_id=3, summary_id=7) is None
    summary_repo.get_aggregation_source_bundle_mock.assert_not_awaited()
