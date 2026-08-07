"""Summary index port -- synchronous read-your-writes vector indexing (ADR-0012).

The summarize graph's ``persist`` node calls this AFTER the summary row exists
and BEFORE the request is marked done, so a subsequent request's ``ground`` node
retrieves the new summary immediately -- without waiting for the next reconciler
pass (freshness; the reconciler remains the convergence/backfill path).

Like every application port this is typed against the application tier only, so
``application-no-outward`` stays green: the persist node depends on this Protocol,
never on the concrete Qdrant adapter (wired at :mod:`app.di.graphs`).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from app.application.dto.vector_search import RetrievalScope

logger = logging.getLogger(__name__)


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
        """Embed + upsert the summary's Qdrant point using the shared point shape.

        Implementations build the point from
        :mod:`app.infrastructure.vector.summary_point` so it is byte-identical to
        the reconciler's point for the same summary (no drift).
        May raise on a vector-store failure; the persist node treats indexing as
        best-effort and never lets it block request completion (ADR-0012).
        """
        ...


async def delete_summary_vectors(vector_store: Any, request_ids: Sequence[int]) -> bool:
    """Drop the Qdrant points for *request_ids*. Returns whether the delete ran.

    The un-index counterpart of :class:`SummaryIndexPort`, and the single
    definition of what deleting a summary means for the vector store -- every
    delete entrypoint calls this, because the one that did not (mobile sync)
    left its point serving the deleted content indefinitely.

    Best-effort by the same ADR-0012 reasoning as the write path: a vector-store
    failure must not fail the user's delete. It returns False instead of raising,
    and the reconciler's prune pass is what makes the deletion eventually
    consistent when this call does not land.

    ``vector_store`` is duck-typed rather than a Protocol so this module stays
    inside the application tier (``application-no-outward``); the concrete store
    is wired at the composition root.
    """
    ids = [int(request_id) for request_id in request_ids]
    if vector_store is None or not ids:
        return False
    try:
        await asyncio.to_thread(vector_store.delete_by_request_ids, ids)
    except Exception:
        logger.warning(
            "summary_vector_delete_failed",
            extra={"request_count": len(ids)},
            exc_info=True,
        )
        return False
    return True
