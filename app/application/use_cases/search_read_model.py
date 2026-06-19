"""Application read-model use case for API search endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.use_cases._tracing import use_case_span

if TYPE_CHECKING:
    from datetime import datetime

    from app.application.ports.requests import RequestRepositoryPort
    from app.application.ports.search import TopicSearchRepositoryPort
    from app.application.ports.summaries import SummaryRepositoryPort


class SearchReadModelUseCase:
    """Provides search/query data access for API presentation adapters."""

    def __init__(
        self,
        topic_search_repository: TopicSearchRepositoryPort,
        request_repository: RequestRepositoryPort,
        summary_repository: SummaryRepositoryPort,
    ) -> None:
        self._topic_search_repo = topic_search_repository
        self._request_repo = request_repository
        self._summary_repo = summary_repository

    async def fts_search_paginated(
        self, query: str, *, limit: int = 20, offset: int = 0, user_id: int | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        with use_case_span(
            "search_read_model.fts_search_paginated",
            user_id=user_id,
            attributes={"ratatoskr.search.limit": limit, "ratatoskr.search.offset": offset},
        ):
            return await self._topic_search_repo.async_fts_search_paginated(
                query, limit=limit, offset=offset, user_id=user_id
            )

    async def get_requests_by_ids(
        self, request_ids: list[int], *, user_id: int | None = None
    ) -> dict[int, dict[str, Any]]:
        with use_case_span(
            "search_read_model.get_requests_by_ids",
            user_id=user_id,
            attributes={"ratatoskr.request.count": len(request_ids)},
        ):
            return await self._request_repo.async_get_requests_by_ids(request_ids, user_id=user_id)

    async def get_summaries_by_request_ids(
        self, request_ids: list[int]
    ) -> dict[int, dict[str, Any]]:
        with use_case_span(
            "search_read_model.get_summaries_by_request_ids",
            attributes={"ratatoskr.request.count": len(request_ids)},
        ):
            return await self._summary_repo.async_get_summaries_by_request_ids(request_ids)

    async def get_user_summaries(
        self, user_id: int, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int, int]:
        with use_case_span(
            "search_read_model.get_user_summaries",
            user_id=user_id,
            attributes={"ratatoskr.search.limit": limit, "ratatoskr.search.offset": offset},
        ):
            return await self._summary_repo.async_get_user_summaries(
                user_id=user_id,
                limit=limit,
                offset=offset,
            )

    async def get_duplicate_request_and_summary(
        self, *, user_id: int, dedupe_hash: str
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        with use_case_span("search_read_model.get_duplicate_request_and_summary", user_id=user_id):
            request = await self._request_repo.async_get_request_by_dedupe_hash(dedupe_hash)
            if not request or request.get("user_id") != user_id:
                return None, None

            summary = await self._summary_repo.async_get_summary_by_request(request["id"])
            return request, summary

    async def get_search_insight_rows(
        self, *, user_id: int, previous_start: datetime, limit: int
    ) -> list[dict[str, Any]]:
        with use_case_span(
            "search_read_model.get_search_insight_rows",
            user_id=user_id,
            attributes={"ratatoskr.search.limit": limit},
        ):
            return await self._summary_repo.async_get_user_summaries_for_insights(
                user_id=user_id,
                request_created_after=previous_start,
                limit=limit,
            )
