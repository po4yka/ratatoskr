"""Vector store protocol and shared exceptions."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.infrastructure.vector.result_types import VectorQueryResult


class VectorStoreError(Exception):
    """Base exception for vector store errors."""


class VectorStoreUnavailableError(VectorStoreError):
    """Raised when the vector store is not reachable or not yet initialised."""


@runtime_checkable
class VectorStore(Protocol):
    """Protocol shared by all vector store backends."""

    @property
    def available(self) -> bool:
        """True when the store is connected and ready."""
        ...

    @property
    def environment(self) -> str: ...

    @property
    def user_scope(self) -> str: ...

    @property
    def collection_version(self) -> str: ...

    @property
    def embedding_space(self) -> str | None: ...

    @property
    def collection_name(self) -> str: ...

    def ensure_available(self) -> bool:
        """Attempt a reconnect if the store is not available.

        Returns True when the store is ready after this call.
        """
        ...

    def health_check(self) -> bool: ...

    def upsert_notes(
        self,
        vectors: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]],
        ids: Sequence[str] | None = None,
        *,
        wait: bool = True,
    ) -> None: ...

    def replace_request_notes(
        self,
        request_id: int | str,
        vectors: Sequence[Sequence[float]],
        metadatas: Sequence[dict[str, Any]],
        ids: Sequence[str] | None = None,
        *,
        wait: bool = True,
    ) -> None: ...

    def query(
        self,
        query_vector: Sequence[float],
        filters: dict[str, Any] | None,
        top_k: int,
    ) -> VectorQueryResult: ...

    def delete_by_request_id(self, request_id: int | str) -> None: ...

    def get_indexed_summary_ids(
        self,
        *,
        user_id: int | None = None,
        limit: int | None = 5000,
    ) -> set[int]: ...

    def count(self) -> int: ...

    def reset(self) -> None: ...

    def close(self) -> None: ...

    async def aclose(self) -> None: ...
