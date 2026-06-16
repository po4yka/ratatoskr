"""``extract`` node -- fetch content via the extraction port (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node
from app.application.ports.extraction import ExtractionRequest

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("extract")
async def extract(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Fetch source content through ``deps.extraction`` (one port that dispatches
    by URL pattern to the scraper chain / youtube / twitter / academic / github /
    meta internally) and project the result into minimal id-based state.

    Only ids/handles + the (single) source-text field are written: the bulk
    ``content_text`` lands in ``state['source_text']`` (consumed by ground +
    summarize); ``dedupe_hash`` / ``content_source`` / ``detected_lang`` /
    ``title`` are small primitives. An extraction failure raises ``ValueError``
    (already persisted via ``persist_request_failure`` inside the adapter), which
    the runner routes to the single terminal-failure path (ADR-0011) -- there is
    NO parallel error path here.
    """
    request_id = state.get("request_id")
    url = (state.get("input_url") or "").strip()
    if not url:
        # No URL settled (e.g. a forwarded message with no link): nothing to
        # fetch. Leave state untouched; downstream nodes see empty source_text.
        return {}

    result = await deps.extraction.extract(
        ExtractionRequest(
            url=url,
            request_id=request_id,
            correlation_id=state.get("correlation_id"),
        )
    )

    return {
        "source_text": result.content_text,
        "content_source": result.content_source,
        "detected_lang": result.detected_lang,
        "dedupe_hash": result.dedupe_hash,
        "title": result.title or "",
    }
