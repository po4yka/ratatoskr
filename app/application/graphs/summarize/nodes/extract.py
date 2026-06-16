"""``extract`` node -- fetch content via the extraction port (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("extract")
async def extract(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Fetch source content through ``deps.extraction`` (one port, dispatches by
    source kind to scraper chain / youtube / twitter / academic internally).

    STUB (T5): returns no update. The extraction-port call + minimal id-based
    persistence (content re-fetched by ``request_id`` downstream) land in T7.
    """
    return {}
