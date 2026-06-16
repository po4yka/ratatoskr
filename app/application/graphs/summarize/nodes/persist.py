"""``persist`` node -- write summaries + llm_calls + read-your-writes index (ADR-0011/0012/0015)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

from app.application.dto.vector_search import RetrievalScope
from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState
    from app.application.ports.requests import LLMCallRecord

logger = logging.getLogger(__name__)


@graph_node("persist")
async def persist(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Persist the summary, the llm_calls, and finalize the request.

    persist-everything (ADR-0011): writes the ``summaries`` row + flips the
    request to COMPLETED via ``async_finalize_request_summary``, then writes every
    accumulated ``llm_calls`` record (``attempt_trigger='graph_node'``, correlation
    id on each). No-ops when nothing was produced (no summary), so the skeleton
    still drains under the generic node tests.

    Then T6's read-your-writes vector fast-path (ADR-0012): once the summary row
    exists -- and BEFORE the request is considered done by downstream pollers --
    the summary is indexed into Qdrant synchronously so a subsequent request's
    ``ground`` node sees it without waiting for the CocoIndex poll. Best-effort:
    a vector-store failure is logged (with ``correlation_id``) and left to the
    reconciler; the summary row is the source of truth and completion is never
    blocked. Not gated by ``SUMMARIZE_RAG_ENABLED`` -- every persisted summary
    should be queryable (search/MCP/grounding), independent of RAG grounding.
    """
    summary = state.get("summary") or {}
    request_id = state.get("request_id")
    if not summary or request_id is None:
        return {}

    lang = state.get("lang") or "en"
    insights = summary.get("insights") if isinstance(summary.get("insights"), dict) else None

    await deps.summaries.async_finalize_request_summary(
        request_id=request_id,
        lang=lang,
        json_payload=summary,
        insights_json=insights,
        is_read=False,
    )

    summary_id = state.get("summary_id")
    try:
        fetched = await deps.summaries.async_get_summary_id_by_request(request_id)
        if isinstance(fetched, int):
            summary_id = fetched
    except Exception:  # best-effort: id lookup must not block completion
        logger.warning(
            "graph_persist_summary_id_lookup_failed",
            extra={"correlation_id": state.get("correlation_id"), "request_id": request_id},
            exc_info=True,
        )

    await _persist_llm_calls(state, deps)
    await _index_summary_for_freshness(state, deps, summary_id=summary_id)

    return {"summary_id": summary_id} if summary_id is not None else {}


async def _persist_llm_calls(state: SummarizeState, deps: SummarizeDeps) -> None:
    """Write the accumulated llm_calls (persist-everything); best-effort per row."""
    if deps.llm_repo is None:
        return
    for record in state.get("llm_calls") or []:
        try:
            await deps.llm_repo.async_insert_llm_call(cast("LLMCallRecord", record))
        except Exception:  # one bad row must not block the rest / completion
            logger.warning(
                "graph_persist_llm_call_failed",
                extra={
                    "correlation_id": state.get("correlation_id"),
                    "request_id": state.get("request_id"),
                },
                exc_info=True,
            )


async def _index_summary_for_freshness(
    state: SummarizeState, deps: SummarizeDeps, *, summary_id: int | None
) -> None:
    """Synchronous index-on-write; swallow vector-store failures (ADR-0012)."""
    summary = state.get("summary") or {}
    request_id = state.get("request_id")
    user_scope = state.get("user_scope")
    environment = state.get("environment")
    if not summary or summary_id is None or request_id is None or not user_scope or not environment:
        # Nothing persisted yet or scope unavailable -- the reconciler / CocoIndex
        # still converge later.
        return

    # Owner-wide summary point (no user_id in the payload -- matches CocoIndex);
    # user_scope + environment are the partition the index writes + ground reads.
    scope = RetrievalScope(environment=environment, user_scope=user_scope, user_id=None)
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
