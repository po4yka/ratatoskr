"""Service for hybrid search combining full-text and vector search."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol, cast

from app.application.dto.topic_search import TopicArticle
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.application.services.topic_search import LocalTopicSearchService
    from app.infrastructure.search.query_expansion_service import QueryExpansionService
    from app.infrastructure.search.search_filters import SearchFilters
    from app.infrastructure.search.vector_search_service import StoreVectorSearchResult


class RerankerProtocol(Protocol):
    async def rerank(
        self,
        query: str,
        results: list[dict[str, Any]],
        *,
        text_field: str = ...,
        title_field: str = ...,
        id_field: str | None = ...,
        score_field: str = ...,
    ) -> list[dict[str, Any]]: ...


logger = get_logger(__name__)


class HybridSearchService:
    """Combines PostgreSQL full-text and vector semantic search."""

    def __init__(
        self,
        fts_service: LocalTopicSearchService,
        vector_service: Any | None,
        *,
        fts_weight: float = 0.4,
        vector_weight: float = 0.6,
        max_results: int = 25,
        query_expansion: QueryExpansionService | None = None,
        reranking: RerankerProtocol | None = None,
    ) -> None:
        if not 0.0 <= fts_weight <= 1.0:
            msg = "fts_weight must be between 0.0 and 1.0"
            raise ValueError(msg)
        if not 0.0 <= vector_weight <= 1.0:
            msg = "vector_weight must be between 0.0 and 1.0"
            raise ValueError(msg)
        if max_results <= 0:
            msg = "max_results must be positive"
            raise ValueError(msg)

        self._fts = fts_service
        self._vector = vector_service
        self._fts_weight = fts_weight
        self._vector_weight = vector_weight
        self._max_results = max_results
        self._query_expansion = query_expansion
        self._reranking = reranking

    async def search(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
        correlation_id: str | None = None,
    ) -> list[TopicArticle]:
        if not query or not query.strip():
            logger.warning("empty_query_for_hybrid_search", extra={"cid": correlation_id})
            return []

        fts_query = query.strip()
        if self._query_expansion:
            expanded = self._query_expansion.expand_for_fts(fts_query)
            logger.debug(
                "query_expanded_for_fts",
                extra={"cid": correlation_id, "original": fts_query, "expanded": expanded},
            )
            fts_query = expanded

        fts_task = asyncio.create_task(
            self._fts.find_articles(fts_query, correlation_id=correlation_id)
        )
        if self._vector is None:
            logger.info("hybrid_search_vector_disabled", extra={"cid": correlation_id})
            fts_results = await fts_task
            vector_results = []
        else:
            vector_task = asyncio.create_task(
                self._vector.search(
                    query.strip(),
                    language=getattr(filters, "language", None) if filters else None,
                    user_scope=getattr(self._vector, "user_scope", None),
                    correlation_id=correlation_id,
                )
            )
            fts_results, vector_search_results = await asyncio.gather(fts_task, vector_task)
            vector_results = list(getattr(vector_search_results, "results", []))

        if filters and filters.has_filters():
            fts_results = [result for result in fts_results if filters.matches(result)]

        combined = self._combine_results(fts_results, vector_results)
        combined.sort(key=lambda item: item["combined_score"], reverse=True)

        if self._reranking:
            top_candidates = combined[: self._max_results * 2]
            if top_candidates:
                reranked = await self._reranking.rerank(
                    query=query.strip(),
                    results=top_candidates,
                    text_field="text",
                    title_field="title",
                )
                combined = reranked if reranked else combined

        articles = [
            TopicArticle(
                title=result["title"],
                url=result["url"],
                snippet=result["snippet"],
                source=result.get("source"),
                published_at=result.get("published_at"),
            )
            for result in combined[: self._max_results]
        ]

        logger.info(
            "hybrid_search_completed",
            extra={
                "cid": correlation_id,
                "query_length": len(query),
                "fts_results": len(fts_results),
                "vector_results": len(vector_results),
                "combined_unique": len(combined),
                "returned_results": len(articles),
                "reranking_used": bool(self._reranking),
                "filters": str(filters) if filters else "none",
            },
        )
        return articles

    def _combine_results(
        self,
        fts_results: list[TopicArticle],
        vector_results: list[StoreVectorSearchResult],
    ) -> list[dict[str, Any]]:
        fts_scores: dict[str, float] = {}
        fts_data: dict[str, TopicArticle] = {}
        for idx, result in enumerate(fts_results):
            score = 1.0 - (idx / max(len(fts_results), 1))
            result_id = result.url or f"fts:{idx}"
            existing = fts_scores.get(result_id)
            if existing is None or score > existing:
                fts_scores[result_id] = score
                fts_data[result_id] = result

        vector_scores: dict[str, float] = {}
        vector_data: dict[str, StoreVectorSearchResult] = {}
        for vector_result in vector_results:
            result_id = self._vector_result_id(vector_result)
            if result_id:
                score = float(getattr(vector_result, "similarity_score", 0.0))
                existing = vector_scores.get(result_id)
                if existing is None or score > existing:
                    vector_scores[result_id] = score
                    vector_data[result_id] = vector_result

        all_ids = set(vector_scores.keys()) | set(fts_scores.keys())
        combined = []
        for result_id in all_ids:
            fts_match = fts_data.get(result_id)
            vector_match = vector_data.get(result_id)

            url = getattr(vector_match, "url", None) or (fts_match.url if fts_match else None)
            title = getattr(vector_match, "title", None) or (fts_match.title if fts_match else None)
            snippet = getattr(vector_match, "snippet", None) or (
                fts_match.snippet if fts_match else None
            )
            text = getattr(vector_match, "text", None) or snippet
            source = getattr(vector_match, "source", None) or (
                fts_match.source if fts_match else None
            )
            published_at = getattr(vector_match, "published_at", None) or (
                fts_match.published_at if fts_match else None
            )

            fts_score = fts_scores.get(url or result_id, 0.0)
            vector_score = vector_scores.get(result_id, 0.0)
            combined_score = self._fts_weight * fts_score + self._vector_weight * vector_score

            combined.append(
                {
                    "id": result_id,
                    "url": url or result_id,
                    "title": title or (url or result_id),
                    "snippet": snippet or text,
                    "text": text or snippet,
                    "source": source,
                    "published_at": published_at,
                    "combined_score": combined_score,
                    "fts_score": fts_score,
                    "vector_score": vector_score,
                    "window_id": getattr(vector_match, "window_id", None) if vector_match else None,
                    "window_index": getattr(vector_match, "window_index", None)
                    if vector_match
                    else None,
                    "chunk_id": getattr(vector_match, "chunk_id", None) if vector_match else None,
                }
            )
        return combined

    @staticmethod
    def _vector_result_id(result: StoreVectorSearchResult) -> str | None:
        return cast("str | None", getattr(result, "url", None) or getattr(result, "chunk_id", None))
