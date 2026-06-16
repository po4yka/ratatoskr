"""``validate`` node -- check the summary against the contract (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("validate")
async def validate(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Validate ``state['summary']`` against the strict summary contract.

    STUB (T5): reports no errors (valid), so the skeleton routes validate ->
    enrich. The real contract validation (``app.core.summary_contract``) that
    populates ``validation_errors`` and drives the validate -> repair loop lands
    in T7.
    """
    return {"validation_errors": []}
