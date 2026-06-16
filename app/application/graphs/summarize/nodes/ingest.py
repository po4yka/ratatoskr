"""``ingest`` node -- normalize URL, compute dedupe_hash, establish ids (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("ingest")
async def ingest(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Entry node: settle request identity (correlation_id / request_id, sacred).

    STUB (T5): identity is supplied in the initial state by the runner, so this
    returns no update. URL normalization + ``dedupe_hash`` + request creation land
    in T7 (ADR-0015), reusing ``app.core.url_utils``.
    """
    return {}
