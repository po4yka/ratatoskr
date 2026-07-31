"""Semantic search using vector embeddings."""

from __future__ import annotations

import asyncio
import heapq
import math
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.lang import detect_language
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.application.ports.search import EmbeddingRepositoryPort, TopicSearchRepositoryPort
    from app.infrastructure.embedding.embedding_protocol import EmbeddingServiceProtocol
    from app.infrastructure.search.search_filters import SearchFilters

logger = get_logger(__name__)


class VectorSearchResult(BaseModel):
    """Result from vector similarity search."""

    model_config = ConfigDict(frozen=True)

    request_id: int
    summary_id: int
    similarity_score: float = Field(ge=0.0, le=1.0)
    url: str | None
    title: str | None
    snippet: str | None
    source: str | None = None
    published_at: str | None = None


class VectorSearchService:
    """Semantic search using vector embeddings."""

    def __init__(
        self,
        *,
        embedding_repository: EmbeddingRepositoryPort,
        topic_search_repository: TopicSearchRepositoryPort,
        embedding_service: EmbeddingServiceProtocol,
        max_results: int = 25,
        min_similarity: float = 0.3,
        candidate_multiplier: int = 40,
        fallback_scan_limit: int = 5000,
    ) -> None:
        if max_results <= 0:
            msg = "max_results must be positive"
            raise ValueError(msg)
        if not 0.0 <= min_similarity <= 1.0:
            msg = "min_similarity must be between 0.0 and 1.0"
            raise ValueError(msg)
        if candidate_multiplier <= 0:
            msg = "candidate_multiplier must be positive"
            raise ValueError(msg)
        if fallback_scan_limit <= 0:
            msg = "fallback_scan_limit must be positive"
            raise ValueError(msg)

        self._repo = embedding_repository
        self._topic_repo = topic_search_repository
        self._embedding_service = embedding_service
        self._max_results = max_results
        self._min_similarity = min_similarity
        self._candidate_multiplier = candidate_multiplier
        self._fallback_scan_limit = fallback_scan_limit

    async def search(
        self,
        query: str,
        *,
        filters: SearchFilters | None = None,
        correlation_id: str | None = None,
    ) -> list[VectorSearchResult]:
        if not query or not query.strip():
            logger.warning("empty_query_for_vector_search", extra={"cid": correlation_id})
            return []

        query_language = detect_language(query)
        try:
            query_embedding = await self._embedding_service.generate_embedding(
                query.strip(), language=query_language, task_type="query"
            )
        except (RuntimeError, ValueError, OSError):
            logger.exception(
                "query_embedding_generation_failed",
                extra={"cid": correlation_id, "query": query[:100], "language": query_language},
            )
            return []

        candidate_limit = max(self._max_results * self._candidate_multiplier, self._max_results)
        candidate_request_ids = await self._topic_repo.async_search_request_ids(
            query,
            candidate_limit=candidate_limit,
        )

        if candidate_request_ids:
            candidates = await self._fetch_embeddings_by_request_ids(candidate_request_ids)
        else:
            candidates = await self._fetch_recent_embeddings(limit=self._fallback_scan_limit)

        if not candidates:
            logger.warning("no_embeddings_available", extra={"cid": correlation_id})
            return []

        results = await asyncio.to_thread(self._compute_similarities, query_embedding, candidates)
        filtered = [result for result in results if result.similarity_score >= self._min_similarity]

        if filters and filters.has_filters():
            filtered = [result for result in filtered if filters.matches(result)]

        filtered_count = len(filtered)
        top_results = heapq.nlargest(
            self._max_results,
            filtered,
            key=lambda result: result.similarity_score,
        )
        logger.info(
            "vector_search_completed",
            extra={
                "cid": correlation_id,
                "query_length": len(query),
                "total_candidates": len(candidates),
                "filtered_results": filtered_count,
                "returned_results": len(top_results),
                "filters": str(filters) if filters else "none",
            },
        )
        return top_results

    async def _fetch_embeddings_by_request_ids(
        self, request_ids: list[int]
    ) -> list[dict[str, Any]]:
        rows = await self._repo.async_get_embeddings_by_request_ids(request_ids)
        return self._materialize_candidates(rows)

    async def _fetch_recent_embeddings(self, *, limit: int) -> list[dict[str, Any]]:
        rows = await self._repo.async_get_recent_embeddings(limit=limit)
        return self._materialize_candidates(rows)

    def _materialize_candidates(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = []
        for row in rows:
            try:
                embedding = self._embedding_service.deserialize_embedding(row["embedding_blob"])
                payload = row["json_payload"] or {}
                metadata = payload.get("metadata", {}) if isinstance(payload, dict) else {}

                url = (
                    metadata.get("canonical_url")
                    or metadata.get("url")
                    or row.get("normalized_url")
                    or row.get("input_url")
                )
                title = metadata.get("title") or payload.get("title")
                snippet = (
                    payload.get("summary_250") or payload.get("tldr") or payload.get("summary_1000")
                )
                if snippet and len(snippet) > 300:
                    snippet = snippet[:297] + "..."

                source = metadata.get("domain") or metadata.get("source")
                published_at = (
                    metadata.get("published_at")
                    or metadata.get("published")
                    or metadata.get("last_updated")
                )

                results.append(
                    {
                        "request_id": row["request_id"],
                        "summary_id": row["summary_id"],
                        "embedding": embedding,
                        "url": url,
                        "title": title or url,
                        "snippet": snippet,
                        "source": source,
                        "published_at": published_at,
                    }
                )
            except (ValueError, KeyError, AttributeError, TypeError):
                logger.exception(
                    "failed_to_process_embedding_row",
                    extra={"summary_id": row.get("summary_id")},
                )
        return results

    def _compute_similarities(
        self,
        query_embedding: Any,
        candidates: list[dict[str, Any]],
    ) -> list[VectorSearchResult]:
        try:
            return self._compute_similarities_vectorized(query_embedding, candidates)
        except (ImportError, ValueError, TypeError):
            logger.debug("vectorized_similarity_failed", exc_info=True)
            return self._compute_similarities_scalar(query_embedding, candidates)

    def _compute_similarities_vectorized(
        self,
        query_embedding: Any,
        candidates: list[dict[str, Any]],
    ) -> list[VectorSearchResult]:
        import numpy as np

        if not candidates:
            return []

        query_vector = np.asarray(query_embedding, dtype=np.float32)
        matrix = np.asarray([candidate["embedding"] for candidate in candidates], dtype=np.float32)
        if matrix.ndim != 2 or query_vector.ndim != 1 or matrix.shape[1] != query_vector.shape[0]:
            msg = "Embedding dimensions do not match"
            raise ValueError(msg)

        query_norm = float(np.linalg.norm(query_vector))
        row_norms = np.linalg.norm(matrix, axis=1)
        denominators = row_norms * query_norm
        dots = matrix @ query_vector

        results: list[VectorSearchResult] = []
        for candidate, dot, denominator in zip(candidates, dots, denominators, strict=True):
            if denominator <= 0 or not math.isfinite(float(denominator)):
                similarity = 0.0
            else:
                similarity = float(dot / denominator)
            results.append(
                VectorSearchResult(
                    request_id=candidate["request_id"],
                    summary_id=candidate["summary_id"],
                    similarity_score=max(0.0, min(1.0, similarity)),
                    url=candidate.get("url"),
                    title=candidate.get("title"),
                    snippet=candidate.get("snippet"),
                    source=candidate.get("source"),
                    published_at=candidate.get("published_at"),
                )
            )
        return results

    def _compute_similarities_scalar(
        self,
        query_embedding: Any,
        candidates: list[dict[str, Any]],
    ) -> list[VectorSearchResult]:
        from scipy.spatial.distance import cosine

        results = []
        for candidate in candidates:
            try:
                distance = cosine(query_embedding, candidate["embedding"])
                similarity = max(0.0, min(1.0, 1.0 - float(distance)))
                results.append(
                    VectorSearchResult(
                        request_id=candidate["request_id"],
                        summary_id=candidate["summary_id"],
                        similarity_score=float(similarity),
                        url=candidate.get("url"),
                        title=candidate.get("title"),
                        snippet=candidate.get("snippet"),
                        source=candidate.get("source"),
                        published_at=candidate.get("published_at"),
                    )
                )
            except (ValueError, TypeError, KeyError):
                logger.exception(
                    "similarity_computation_failed",
                    extra={"summary_id": candidate.get("summary_id")},
                )
        return results


# ---------------------------------------------------------------------------
# Store-backed vector search
# ---------------------------------------------------------------------------


class StoreVectorSearchResult(BaseModel):
    """Normalized result returned by vector store queries."""

    model_config = ConfigDict(frozen=True)

    request_id: int
    summary_id: int
    user_id: int | None = None
    similarity_score: float = Field(ge=0.0, le=1.0)
    url: str | None = None
    title: str | None = None
    snippet: str | None = None
    text: str | None = None
    source: str | None = None
    published_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    language: str | None = None
    window_id: str | None = None
    window_index: int | None = None
    chunk_id: str | None = None
    neighbor_chunk_ids: list[str] = Field(default_factory=list)
    semantic_boosters: list[str] = Field(default_factory=list)
    local_keywords: list[str] = Field(default_factory=list)
    local_summary: str | None = None
    query_expansion_keywords: list[str] = Field(default_factory=list)
    section: str | None = None
    topics: list[str] = Field(default_factory=list)


class StoreVectorSearchResults(BaseModel):
    """Collection of vector search results with pagination hints."""

    model_config = ConfigDict(frozen=True)

    results: list[StoreVectorSearchResult]
    has_more: bool = False


class StoreVectorSearchService:
    """Run semantic search queries against a vector store using request metadata."""

    def __init__(
        self,
        *,
        vector_store: Any,
        embedding_service: EmbeddingServiceProtocol,
        default_top_k: int = 25,
    ) -> None:
        if default_top_k <= 0:
            msg = "default_top_k must be positive"
            raise ValueError(msg)

        self._vector_store = vector_store
        self._embedding_service = embedding_service
        self._default_top_k = default_top_k

    async def search(
        self,
        query: str,
        *,
        language: str | None = None,
        tags: Iterable[str] | None = None,
        user_scope: str | None = None,
        user_id: int | None = None,
        limit: int | None = None,
        offset: int = 0,
        correlation_id: str | None = None,
    ) -> StoreVectorSearchResults:
        """Search the vector store for summaries similar to the query text."""

        if not query or not query.strip():
            logger.warning("vector_search_empty_query")
            return StoreVectorSearchResults(results=[], has_more=False)

        requested_limit = limit or self._default_top_k
        fetch_limit = requested_limit + offset + 1  # +1 to detect has_more

        detected_language = language or detect_language(query)

        try:
            query_embedding = await self._embedding_service.generate_embedding(
                query.strip(), language=detected_language, task_type="query"
            )
        except Exception:
            logger.exception(
                "vector_search_embedding_failed",
                extra={"language": detected_language},
            )
            return StoreVectorSearchResults(results=[], has_more=False)

        store_scope = getattr(self._vector_store, "user_scope", None) or "public"
        if user_scope and user_scope != store_scope:
            logger.warning(
                "vector_search_scope_mismatch",
                extra={"requested_scope": user_scope, "store_scope": store_scope},
            )
            return StoreVectorSearchResults(results=[], has_more=False)

        filters = {
            "language": detected_language,
            "tags": list(tags) if tags else [],
            "user_id": user_id,
        }

        try:
            query_result = await asyncio.to_thread(
                self._vector_store.query,
                query_embedding,
                filters,
                fetch_limit,
            )
        except Exception:
            logger.exception("vector_search_unexpected_error")
            return StoreVectorSearchResults(results=[], has_more=False)

        if not query_result.hits:
            return StoreVectorSearchResults(results=[], has_more=False)

        results: list[StoreVectorSearchResult] = []

        for hit in query_result.hits:
            metadata = hit.metadata
            if not isinstance(metadata, dict):
                continue

            request_id = self._safe_int(metadata.get("request_id"))
            summary_id = self._safe_int(metadata.get("summary_id"))

            if request_id is None or summary_id is None:
                continue

            similarity_score = max(0.0, min(1.0, 1.0 - hit.distance))
            raw_text = metadata.get("text")
            snippet = metadata.get("local_summary") or raw_text
            if snippet and len(str(snippet)) > 300:
                snippet = str(snippet)[:297] + "..."

            results.append(
                StoreVectorSearchResult(
                    request_id=request_id,
                    summary_id=summary_id,
                    user_id=self._safe_int(metadata.get("user_id")),
                    similarity_score=similarity_score,
                    url=metadata.get("url"),
                    title=metadata.get("title"),
                    snippet=snippet,
                    text=raw_text,
                    source=metadata.get("source"),
                    published_at=metadata.get("published_at"),
                    tags=self._normalize_tags(metadata.get("tags")),
                    language=metadata.get("language"),
                    window_id=metadata.get("window_id"),
                    window_index=self._safe_int(metadata.get("window_index")),
                    chunk_id=metadata.get("chunk_id"),
                    neighbor_chunk_ids=self._normalize_tags(metadata.get("neighbor_chunk_ids")),
                    semantic_boosters=self._normalize_tags(metadata.get("semantic_boosters")),
                    local_keywords=self._normalize_tags(metadata.get("local_keywords")),
                    local_summary=metadata.get("local_summary"),
                    query_expansion_keywords=self._normalize_tags(
                        metadata.get("query_expansion_keywords")
                    ),
                    section=metadata.get("section"),
                    topics=self._normalize_tags(metadata.get("topics")),
                )
            )

        has_more = len(results) > offset + requested_limit
        return StoreVectorSearchResults(
            results=results[offset : offset + requested_limit],
            has_more=has_more,
        )

    @staticmethod
    def _safe_int(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_tags(tags: Any) -> list[str]:
        if not tags:
            return []
        if isinstance(tags, str):
            return [tags]
        if isinstance(tags, list | tuple | set):
            clean: list[str] = []
            for tag in tags:
                text = str(tag).strip()
                if text:
                    clean.append(text)
            return clean
        return []
