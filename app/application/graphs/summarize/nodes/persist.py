"""``persist`` node -- write llm_calls + summaries + read-your-writes index (ADR-0011/0012/0015)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.application.dto.vector_search import RetrievalScope
from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState

logger = logging.getLogger(__name__)


@graph_node("persist")
async def persist(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Persist the summary and finalize the request (persist-everything invariant).

    STUB (T7): writing ``summaries`` via ``deps.summaries`` and ``llm_calls``
    (incl. failures, ``attempt_trigger='graph_node'``) with the correlation id on
    every row, and marking the request done, land in T7.

    T6 owns the read-your-writes vector fast-path (ADR-0012): once the summary row
    exists (``summary_id`` present) -- and BEFORE the request is marked done -- the
    summary is indexed into Qdrant synchronously, so a subsequent request's
    ``ground`` node sees it immediately without waiting for the CocoIndex poll.
    Best-effort: a vector-store failure is logged (with ``correlation_id``) and
    left to the reconciler; the summary row is the source of truth and request
    completion is never blocked.

    The index-on-write is DELIBERATELY NOT gated by ``SUMMARIZE_RAG_ENABLED``: that
    flag gates RAG *grounding* in the ground node, whereas freshness is an
    independent concern -- every persisted summary should be queryable (search API,
    MCP, future grounding), so it runs whenever the graph persists a summary.
    """
    await _index_summary_for_freshness(state, deps)
    return {}


async def _index_summary_for_freshness(state: SummarizeState, deps: SummarizeDeps) -> None:
    """Synchronous index-on-write; swallow vector-store failures (ADR-0012)."""
    summary = state.get("summary") or {}
    summary_id = state.get("summary_id")
    request_id = state.get("request_id")
    user_scope = state.get("user_scope")
    environment = state.get("environment")
    if not summary or summary_id is None or request_id is None or not user_scope or not environment:
        # Nothing persisted yet (T5/T7 ordering) or scope unavailable -- the
        # reconciler/CocoIndex still converge later.
        return

    # Owner-wide summary point (no user_id in the payload -- matches CocoIndex);
    # user_scope + environment are the partition the index writes + ground reads.
    scope = RetrievalScope(
        environment=environment,
        user_scope=user_scope,
        user_id=None,
    )
    try:
        await deps.summary_index.index_summary(
            request_id=request_id,
            summary_id=summary_id,
            summary=summary,
            lang=state.get("lang"),
            scope=scope,
            correlation_id=state.get("correlation_id"),
        )
    except Exception:  # best-effort: freshness must never block completion (ADR-0012)
        logger.warning(
            "summary_index_fastpath_failed",
            extra={
                "correlation_id": state.get("correlation_id"),
                "request_id": request_id,
                "summary_id": summary_id,
            },
            exc_info=True,
        )
