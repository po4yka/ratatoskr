"""``summarize`` node -- structured summary via the llm_client port (ADR-0006/0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("summarize")
async def summarize(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Produce the structured summary via ``deps.llm_client.chat_structured``.

    STUB (T5): returns no update. The instructor-backed structured call, model
    routing, and ``call_count`` budgeting land in T7; a budget breach raises
    ``CallBudgetExceeded`` (routed to the single terminal-failure path).
    """
    return {}
