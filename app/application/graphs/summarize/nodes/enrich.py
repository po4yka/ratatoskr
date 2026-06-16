"""``enrich`` node -- optional two-pass enrichment (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("enrich")
async def enrich(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Optional second enrichment pass over the validated summary.

    STUB (T5): no-op. The two-pass enrichment (``enrichment_system_{en,ru}.txt``,
    gated by ``cfg.runtime.summary_two_pass_enabled``) lands in T7.
    """
    return {}
