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
        RetrievalResult,
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
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
        rerank: bool = False,
        expand_query: bool = False,
        exclude_request_id: int | None = None,
        correlation_id: str | None = None,
    ) -> RetrievalResult:
        """Return the top-``top_k`` hits for ``query`` OR ``vector``.

        Exactly one of ``query`` (embedded by the adapter) or ``vector`` (a
        precomputed embedding) is supplied. ``scope`` is mandatory and is
        merged with any optional ``filters``; callers cannot bypass it.

        ``exclude_request_id`` drops every point whose ``request_id`` payload
        equals it, via the same centralized filter -- the summarize ``ground``
        node passes the current request so a re-summarization never grounds on
        its own prior summary (ADR-0005/0012). It composes with ``scope``; it is
        not a bypass.

        ``rerank`` and ``expand_query`` default to ``False`` so the result
        ordering reproduces the un-reranked, un-expanded legacy behavior
        exactly; turning either on routes through the adapter's optional
        rerank / query-expansion step.
        """
        ...

    async def find_similar(
        self,
        *,
        entity_type: EntityType,
        entity_id: str,
        scope: RetrievalScope,
        top_k: int = 10,
        correlation_id: str | None = None,
    ) -> RetrievalResult:
        """Return entities similar to the seed ``entity_id`` (seed excluded).

        Uses Qdrant's by-point-id recommendation over the seed's stored vector
        (not a re-embed). ``scope`` is mandatory and filtered identically to
        :meth:`retrieve`. Named ``entity_id`` rather than ``id`` to avoid
        shadowing the builtin.
        """
        ...
