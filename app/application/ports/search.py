"""Topic-search and embedding ports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from app.application.dto.vector_search import VectorSearchHitDTO


class EmbeddingDependencyUnavailableError(RuntimeError):
    """Raised when the embedding backend's native dependencies are unavailable.

    Distinct from a transient embedding failure: this signals a hard
    environment problem (e.g. ``torch``/CUDA shared libraries missing on the
    host) that will not resolve on retry. Callers should degrade quietly --
    one concise warning -- rather than log a traceback for every summary.
    """


@runtime_checkable
class TopicSearchResultItemPort(Protocol):
    """Normalized topic-search item returned by an external search provider."""

    title: str
    url: str
    snippet: str | None
    source: str | None
    published_at: str | None


@runtime_checkable
class TopicSearchResultPort(Protocol):
    """Normalized topic-search response returned by an external search provider."""

    status: str
    http_status: int | None
    results: Iterable[TopicSearchResultItemPort]
    total_results: int | None
    error_text: str | None


@runtime_checkable
class TopicSearchClientPort(Protocol):
    """Port for topic-search providers used by application services."""

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        request_id: int | None = None,
    ) -> TopicSearchResultPort:
        """Search for topic-related articles."""


@runtime_checkable
class TopicSearchRepositoryPort(Protocol):
    """Port for topic search query operations."""

    async def async_fts_search_paginated(
        self, query: str, *, limit: int = 20, offset: int = 0, user_id: int | None = None
    ) -> tuple[list[dict[str, Any]], int]:
        """Execute paginated FTS query, scoped to user_id when provided."""

    async def async_search_request_ids(
        self,
        query: str,
        *,
        candidate_limit: int = 100,
    ) -> list[int] | None:
        """Return request IDs matching the topic query."""

    async def async_search_documents(self, query: str, *, limit: int) -> list[Any]:
        """Return indexed topic-search documents."""

    async def async_scan_documents(
        self,
        *,
        terms: list[str],
        normalized_query: str,
        seen_urls: set[str],
        limit: int,
        max_scan: int,
    ) -> list[Any]:
        """Return fallback-scanned topic-search documents."""

    async def async_refresh_index(self, request_id: int) -> None:
        """Refresh the search index for the supplied request."""

    async def async_update_tags_for_summary(self, summary_id: int) -> None:
        """Refresh indexed tag metadata for the supplied summary."""


@runtime_checkable
class EmbeddingRepositoryPort(Protocol):
    async def async_get_all_embeddings(self) -> list[dict[str, Any]]:
        """Return all summary embeddings."""

    async def async_get_embeddings_by_request_ids(
        self,
        request_ids: list[int],
    ) -> list[dict[str, Any]]:
        """Return embeddings for selected request IDs."""

    async def async_get_recent_embeddings(self, *, limit: int) -> list[dict[str, Any]]:
        """Return recent embeddings."""

    async def async_create_or_update_summary_embedding(
        self,
        summary_id: int,
        embedding_blob: bytes,
        model_name: str,
        model_version: str,
        dimensions: int,
        language: str | None = None,
        content_hash: str | None = None,
    ) -> None:
        """Upsert a summary embedding.

        When ``content_hash`` is supplied, the adapter records the DB embedding
        content and leaves the row pending until the vector writer confirms a
        successful Qdrant upsert.
        """

    async def async_mark_summary_embeddings_indexed(self, summary_ids: list[int]) -> None:
        """Mark summary embeddings as successfully written to the vector store."""

    async def async_get_summary_embedding(self, summary_id: int) -> dict[str, Any] | None:
        """Return summary embedding by summary ID."""


@runtime_checkable
class VectorSearchPort(Protocol):
    async def search(
        self,
        query: str,
        *,
        correlation_id: str | None = None,
    ) -> list[VectorSearchHitDTO]:
        """Return vector-search hits for the query."""


@runtime_checkable
class EmbeddingProviderPort(Protocol):
    async def generate_embedding(
        self,
        text: str,
        *,
        language: str | None = None,
        task_type: str = "document",
    ) -> list[float]:
        """Generate an embedding vector."""

    async def generate_embeddings_batch(
        self,
        texts: Sequence[str],
        *,
        language: str | None = None,
        task_type: str = "document",
    ) -> list[Any]:
        """Generate embeddings for many texts in one batched call."""

    def serialize_embedding(self, embedding: list[float]) -> bytes:
        """Serialize the embedding for persistence."""

    def deserialize_embedding(self, blob: bytes) -> list[float]:
        """Deserialize a persisted embedding blob."""

    def get_model_name(self, language: str | None = None) -> str:
        """Return the effective model name for the requested language."""
