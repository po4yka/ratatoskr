"""``ingest`` node -- normalize URL, compute dedupe_hash, establish ids (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node
from app.core.url_utils import compute_dedupe_hash, normalize_url

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("ingest")
async def ingest(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Normalize URL, compute its dedupe key, and settle request identity.

    The URL facade must create the request row before acquiring its processing
    lease, so it calls :func:`prepare_ingest_update` as a transaction preflight.
    The graph node applies the same pure contract to checkpointed state instead of
    duplicating normalization and identity rules in the adapter.
    """
    return prepare_ingest_update(state)


def prepare_ingest_update(state: SummarizeState) -> dict[str, Any]:
    """Build the deterministic ingest delta without touching adapters or I/O."""
    update: dict[str, Any] = {}

    correlation_id = (state.get("correlation_id") or "").strip()
    if correlation_id:
        update["correlation_id"] = correlation_id

    request_id = state.get("request_id")
    if request_id is not None:
        if request_id <= 0:
            raise ValueError("request_id must be a positive integer")
        update["request_id"] = request_id

    input_url = (state.get("input_url") or "").strip()
    if input_url:
        normalized_url = normalize_url(input_url)
        update["input_url"] = normalized_url
        update["dedupe_hash"] = compute_dedupe_hash(normalized_url)

    return update
