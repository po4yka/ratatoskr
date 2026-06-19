"""Application use case for summary read/write operations used by API adapters."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from app.application.use_cases._tracing import use_case_span

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.application.ports.requests import (
        CrawlResultRepositoryPort,
        LLMRepositoryPort,
        RequestRepositoryPort,
    )
    from app.application.ports.summaries import SummaryRepositoryPort


class SummaryReadModelUseCase:
    """Orchestrates summary operations for presentation adapters.

    This keeps API routers free from direct repository calls.
    """

    def __init__(
        self,
        summary_repository: SummaryRepositoryPort,
        request_repository: RequestRepositoryPort,
        crawl_result_repository: CrawlResultRepositoryPort,
        llm_repository: LLMRepositoryPort,
        vector_store: Any | None = None,
    ) -> None:
        self._summary_repo = summary_repository
        self._request_repo = request_repository
        self._crawl_repo = crawl_result_repository
        self._llm_repo = llm_repository
        self._vector_store = vector_store

    async def get_user_summaries(
        self,
        user_id: int,
        limit: int = 20,
        offset: int = 0,
        is_read: bool | None = None,
        is_favorited: bool | None = None,
        lang: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        sort: str = "created_at_desc",
        search: str | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        with use_case_span(
            "summary_read_model.get_user_summaries",
            user_id=user_id,
            attributes={"ratatoskr.search.limit": limit, "ratatoskr.search.offset": offset},
        ):
            # Normalise empty / whitespace-only search to None so the repo
            # does not run a wildcard ILIKE that matches every row.
            cleaned_search = search.strip() if search else None
            if not cleaned_search:
                cleaned_search = None
            return await self._summary_repo.async_get_user_summaries(
                user_id=user_id,
                limit=limit,
                offset=offset,
                is_read=is_read,
                is_favorited=is_favorited,
                lang=lang,
                start_date=start_date,
                end_date=end_date,
                sort=sort,
                search=cleaned_search,
            )

    _BULK_MAX_IDS = 500

    async def bulk_mark_as_read(self, *, user_id: int, summary_ids: list[int]) -> int:
        """Bulk mark summaries as read for *user_id*.

        Empty input is a no-op (returns 0 without hitting the repo).
        Duplicate IDs are deduplicated in caller order. Batches over
        :attr:`_BULK_MAX_IDS` are rejected to bound the SQL
        ``WHERE id IN (...)`` size.

        Returns the number of rows actually updated by the repository.
        """
        with use_case_span(
            "summary_read_model.bulk_mark_as_read",
            user_id=user_id,
            attributes={"ratatoskr.summary.count": len(summary_ids)},
        ):
            if not summary_ids:
                return 0
            seen: dict[int, None] = {}
            for sid in summary_ids:
                seen.setdefault(sid, None)
            deduped = list(seen)
            if len(deduped) > self._BULK_MAX_IDS:
                raise ValueError(
                    f"bulk_mark_as_read accepts at most {self._BULK_MAX_IDS} ids; got {len(deduped)}"
                )
            return await self._summary_repo.async_bulk_mark_summaries_as_read(
                user_id=user_id, summary_ids=deduped
            )

    async def bulk_set_favorite(self, *, user_id: int, summary_ids: list[int], value: bool) -> int:
        """Bulk set the favorite flag on summaries owned by *user_id*.

        Same dedup + cap + empty-no-op semantics as :meth:`bulk_mark_as_read`.
        """
        with use_case_span(
            "summary_read_model.bulk_set_favorite",
            user_id=user_id,
            attributes={"ratatoskr.summary.count": len(summary_ids)},
        ):
            if not summary_ids:
                return 0
            seen: dict[int, None] = {}
            for sid in summary_ids:
                seen.setdefault(sid, None)
            deduped = list(seen)
            if len(deduped) > self._BULK_MAX_IDS:
                raise ValueError(
                    f"bulk_set_favorite accepts at most {self._BULK_MAX_IDS} ids; got {len(deduped)}"
                )
            return await self._summary_repo.async_bulk_set_summaries_favorite(
                user_id=user_id, summary_ids=deduped, value=value
            )

    async def bulk_delete(self, *, user_id: int, summary_ids: list[int]) -> int:
        """Bulk soft-delete summaries owned by *user_id*."""
        with use_case_span(
            "summary_read_model.bulk_delete",
            user_id=user_id,
            attributes={"ratatoskr.summary.count": len(summary_ids)},
        ):
            if not summary_ids:
                return 0
            seen: dict[int, None] = {}
            for sid in summary_ids:
                seen.setdefault(sid, None)
            deduped = list(seen)
            if len(deduped) > self._BULK_MAX_IDS:
                raise ValueError(
                    f"bulk_delete accepts at most {self._BULK_MAX_IDS} ids; got {len(deduped)}"
                )
            request_ids = await self._request_ids_for_owned_summaries(
                user_id=user_id, summary_ids=deduped
            )
            deleted = await self._summary_repo.async_bulk_soft_delete_summaries(
                user_id=user_id, summary_ids=deduped
            )
            if deleted:
                await self._delete_vectors_by_request_ids(request_ids)
            return deleted

    async def get_summary_by_id_for_user(
        self, user_id: int, summary_id: int
    ) -> dict[str, Any] | None:
        with use_case_span(
            "summary_read_model.get_summary_by_id_for_user",
            user_id=user_id,
            attributes={"ratatoskr.summary.id": summary_id},
        ):
            summary = await self._summary_repo.async_get_summary_by_id(summary_id)
            if not summary:
                return None
            if summary.get("user_id") != user_id or summary.get("is_deleted"):
                return None
            return summary

    async def get_summary_id_by_url_for_user(self, user_id: int, url: str) -> int | None:
        with use_case_span("summary_read_model.get_summary_id_by_url_for_user", user_id=user_id):
            request_id = await self._request_repo.async_get_request_id_by_url_with_summary(
                user_id=user_id,
                url=url,
            )
            if not request_id:
                return None
            return await self._summary_repo.async_get_summary_id_by_request(request_id)

    async def get_request_by_id(self, request_id: int) -> dict[str, Any] | None:
        with use_case_span(
            "summary_read_model.get_request_by_id",
            attributes={"ratatoskr.request.id": request_id},
        ):
            return await self._request_repo.async_get_request_by_id(request_id)

    async def get_crawl_result_by_request(self, request_id: int) -> dict[str, Any] | None:
        with use_case_span(
            "summary_read_model.get_crawl_result_by_request",
            attributes={"ratatoskr.request.id": request_id},
        ):
            return await self._crawl_repo.async_get_crawl_result_by_request(request_id)

    async def get_llm_calls_by_request(self, request_id: int) -> list[dict[str, Any]]:
        with use_case_span(
            "summary_read_model.get_llm_calls_by_request",
            attributes={"ratatoskr.request.id": request_id},
        ):
            return await self._llm_repo.async_get_llm_calls_by_request(request_id)

    async def get_summary_context_for_user(
        self, user_id: int, summary_id: int
    ) -> dict[str, Any] | None:
        with use_case_span(
            "summary_read_model.get_summary_context_for_user",
            user_id=user_id,
            attributes={"ratatoskr.summary.id": summary_id},
        ):
            context = await self._summary_repo.async_get_summary_context_by_id(summary_id)
            if not context:
                return None

            summary = context.get("summary") or {}
            if summary.get("user_id") != user_id or summary.get("is_deleted"):
                return None

            request_data = context.get("request") or {}
            request_id = request_data.get("id") or summary.get("request_id")
            if request_id is None:
                return None

            request_id_int = int(request_id)
            llm_calls = await self._llm_repo.async_get_llm_calls_by_request(request_id_int)
            aggregation_source_bundle = await self._summary_repo.async_get_aggregation_source_bundle_for_summary_owned_by_user(
                summary_id,
                user_id,
            )
            return {
                "summary": summary,
                "request": request_data,
                "request_id": request_id_int,
                "crawl_result": context.get("crawl_result"),
                "transcription_artifact": context.get("transcription_artifact"),
                "llm_calls": llm_calls,
                "aggregation_source_bundle": aggregation_source_bundle,
            }

    async def update_summary(
        self,
        user_id: int,
        summary_id: int,
        is_read: bool | None = None,
    ) -> dict[str, Any] | None:
        with use_case_span(
            "summary_read_model.update_summary",
            user_id=user_id,
            attributes={"ratatoskr.summary.id": summary_id},
        ):
            summary = await self.get_summary_by_id_for_user(user_id=user_id, summary_id=summary_id)
            if not summary:
                return None

            if is_read is not None:
                if is_read:
                    await self._summary_repo.async_mark_summary_as_read(summary_id)
                else:
                    await self._summary_repo.async_mark_summary_as_unread(summary_id)

            return await self._summary_repo.async_get_summary_by_id(summary_id)

    async def update_reading_progress(
        self,
        user_id: int,
        summary_id: int,
        progress: float,
        last_read_offset: int,
    ) -> bool:
        """Update reading progress and offset. Returns False if summary not found/owned."""
        with use_case_span(
            "summary_read_model.update_reading_progress",
            user_id=user_id,
            attributes={"ratatoskr.summary.id": summary_id},
        ):
            summary = await self.get_summary_by_id_for_user(user_id=user_id, summary_id=summary_id)
            if not summary:
                return False

            await self._summary_repo.async_update_reading_progress(
                summary_id, progress, last_read_offset
            )
            return True

    async def soft_delete_summary(self, user_id: int, summary_id: int) -> bool:
        with use_case_span(
            "summary_read_model.soft_delete_summary",
            user_id=user_id,
            attributes={"ratatoskr.summary.id": summary_id},
        ):
            summary = await self.get_summary_by_id_for_user(user_id=user_id, summary_id=summary_id)
            if not summary:
                return False

            await self._summary_repo.async_soft_delete_summary(summary_id)
            request_id = summary.get("request_id")
            if request_id is not None:
                await self._delete_vectors_by_request_ids([int(request_id)])
            return True

    async def toggle_favorite(self, user_id: int, summary_id: int) -> bool | None:
        with use_case_span(
            "summary_read_model.toggle_favorite",
            user_id=user_id,
            attributes={"ratatoskr.summary.id": summary_id},
        ):
            summary = await self.get_summary_by_id_for_user(user_id=user_id, summary_id=summary_id)
            if not summary:
                return None
            return await self._summary_repo.async_toggle_favorite(summary_id)

    async def submit_feedback(
        self,
        user_id: int,
        summary_id: int,
        rating: int | None,
        issues: list[str] | None,
        comment: str | None,
    ) -> dict[str, Any] | None:
        """Submit or update feedback for a summary. Returns the feedback record dict, or None if not found."""
        with use_case_span(
            "summary_read_model.submit_feedback",
            user_id=user_id,
            attributes={"ratatoskr.summary.id": summary_id},
        ):
            context = await self.get_summary_context_for_user(
                user_id=user_id, summary_id=summary_id
            )
            if not context:
                return None
            return await self._summary_repo.async_upsert_feedback(
                user_id=user_id,
                summary_id=summary_id,
                rating=rating,
                issues=issues,
                comment=comment,
            )

    async def _request_ids_for_owned_summaries(
        self, *, user_id: int, summary_ids: list[int]
    ) -> list[int]:
        request_ids: list[int] = []
        seen: set[int] = set()
        for summary_id in summary_ids:
            summary = await self.get_summary_by_id_for_user(user_id=user_id, summary_id=summary_id)
            request_id = summary.get("request_id") if summary else None
            if request_id is None:
                continue
            request_id_int = int(request_id)
            if request_id_int not in seen:
                seen.add(request_id_int)
                request_ids.append(request_id_int)
        return request_ids

    async def _delete_vectors_by_request_ids(self, request_ids: list[int]) -> None:
        vector_store = self._vector_store
        if vector_store is None:
            return
        for request_id in request_ids:
            try:
                await asyncio.to_thread(vector_store.delete_by_request_id, request_id)
            except Exception:
                logger.warning(
                    "summary_vector_delete_failed",
                    extra={"request_id": request_id},
                    exc_info=True,
                )
