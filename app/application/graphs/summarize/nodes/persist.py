"""``persist`` node -- write llm_calls + summaries (ADR-0011/0015/0018)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("persist")
async def persist(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Persist the summary and finalize the request (persist-everything invariant).

    STUB (T5): no-op. Writing ``summaries`` via ``deps.summaries`` and ``llm_calls``
    (incl. failures, ``attempt_trigger='graph_node'``) with the correlation id on
    every row lands in T7.
    """
    return {}
