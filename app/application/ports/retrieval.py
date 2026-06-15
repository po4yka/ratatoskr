"""Unified retrieval port (ADR-0016).

One port, one Qdrant-backed adapter (landed by T4): every vector-retrieval
consumer -- the graph ``ground`` node (ADR-0005/0012), MCP semantic search,
and the API ``/search/*`` endpoints -- goes through ``RetrievalPort`` instead
of re-deriving query embedding, Qdrant query, scope filtering, and hydration.

**Mandatory scope filter.** Every call MUST pass a :class:`RetrievalScope`.
The adapter injects ``environment`` + ``user_scope`` (and per-entity
``user_id`` for user-scoped entities) into the Qdrant filter, so the IDOR /
tenant guard (CLAUDE.md rule 12, ADR-0005/0012) is structurally impossible to
omit -- callers never hand-build scope filters. Both methods are keyword-only
so ``scope`` cannot be skipped positionally.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from app.application.dto.vector_search import (
        EntityType,
        RetrievalHitDTO,
        RetrievalScope,
    )


@runtime_checkable
class RetrievalPort(Protocol):
    """Vector retrieval over scope-filtered, entity-typed corpora."""

    async def retrieve(
        self,
        *,
        entity_type: EntityType,
        scope: RetrievalScope,
        query: str | None = None,
        vector: Sequence[float] | None = None,
        top_k: int = 5,
        filters: Mapping[str, Any] | None = None,
    ) -> list[RetrievalHitDTO]:
        """Return the top-``top_k`` hits for ``query`` OR ``vector``.

        Exactly one of ``query`` (embedded by the adapter) or ``vector`` (a
        precomputed embedding) is supplied. ``scope`` is mandatory and is
        merged with any optional ``filters``; callers cannot bypass it.
        """
        ...

    async def find_similar(
        self,
        *,
        entity_type: EntityType,
        entity_id: str,
        top_k: int = 5,
    ) -> list[RetrievalHitDTO]:
        """Return entities similar to the seed ``entity_id`` (seed excluded).

        Scope filtering is applied identically to :meth:`retrieve`. Named
        ``entity_id`` rather than ``id`` to avoid shadowing the builtin.
        """
        ...
