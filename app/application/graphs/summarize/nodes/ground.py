"""``ground`` node -- optional RAG grounding via the retrieval port (ADR-0005/0012)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("ground")
async def ground(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Retrieve top-k scope-filtered prior summaries via ``deps.retrieval``.

    STUB (T5): no-op (empty grounding). The scope-filtered ``retrieve`` call,
    current-request exclusion, and anti-contamination block land in T6 (ADR-0016),
    gated by ``SUMMARIZE_RAG_ENABLED``.
    """
    return {"grounding_ids": []}
