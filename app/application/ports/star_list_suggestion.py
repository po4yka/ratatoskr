"""Ports for suggesting which star list a repository belongs in.

The nearest-neighbour search is satisfied by
``app.infrastructure.search.repository_search_service.RepositorySearchService`` and
the classifier by
``app.application.services.star_list_classifier.StarListClassifierService``. Both
are injected, so the use case never reaches into the infrastructure layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.core.star_list_suggestion_schema import StarListCandidate, StarListChoice


@runtime_checkable
class NeighbourPort(Protocol):
    """One repository the user has already filed."""

    @property
    def repository_id(self) -> int:
        """Local primary key; used to keep the subject out of its own result set."""
        ...

    @property
    def list_names(self) -> list[str]:
        """Star lists this neighbour belongs to. May be empty."""
        ...

    @property
    def distance(self) -> float:
        """``1 - cosine_similarity``, so lower is more similar."""
        ...


@runtime_checkable
class NeighbourSearchPort(Protocol):
    """Semantic search over the user's own repositories."""

    async def search(
        self,
        query: str,
        *,
        user_id: int,
        limit: int = 20,
        min_similarity: float = 0.2,
        correlation_id: str | None = None,
    ) -> Any:
        """Return an object with an ``items`` sequence of :class:`NeighbourPort`."""
        ...


@runtime_checkable
class StarListClassifierPort(Protocol):
    """LLM fallback used when the neighbours do not agree."""

    async def classify(
        self,
        *,
        candidates: list[StarListCandidate],
        full_name: str,
        description: str | None,
        language: str | None,
        topics: list[str],
        readme_excerpt: str | None,
        correlation_id: str | None = None,
    ) -> StarListChoice | None:
        """Return the model's pick, or ``None`` when it cannot be trusted."""
        ...
