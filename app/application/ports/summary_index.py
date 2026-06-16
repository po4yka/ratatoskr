"""Summary index port -- synchronous read-your-writes vector indexing (ADR-0012).

The summarize graph's ``persist`` node calls this AFTER the summary row exists
and BEFORE the request is marked done, so a subsequent request's ``ground`` node
retrieves the new summary immediately -- without waiting for the CocoIndex poll
(freshness; CocoIndex / the reconciler remain the convergence/backfill path).

Like every application port this is typed against the application tier only, so
``application-no-outward`` stays green: the persist node depends on this Protocol,
never on the concrete Qdrant adapter (wired at :mod:`app.di.graphs`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.application.dto.vector_search import RetrievalScope


@runtime_checkable
class SummaryIndexPort(Protocol):
    """Index a just-persisted summary into the vector store on the write path."""

    async def index_summary(
        self,
        *,
        request_id: int,
        summary_id: int,
        summary: Mapping[str, Any],
        lang: str | None,
        scope: RetrievalScope,
        correlation_id: str | None = None,
    ) -> None:
        """Embed + upsert the summary's Qdrant point, byte-compatible with CocoIndex.

        Implementations build the point from
        :mod:`app.infrastructure.vector.summary_point` so it is byte-identical to
        the CocoIndex flow's point for the same summary (no reconciler drift).
        May raise on a vector-store failure; the persist node treats indexing as
        best-effort and never lets it block request completion (ADR-0012).
        """
        ...
