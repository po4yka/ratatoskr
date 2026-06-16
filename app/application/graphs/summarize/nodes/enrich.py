"""``enrich`` node -- optional two-pass enrichment (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.deps import SummarizeConfig
from app.application.graphs.summarize.nodes._span import graph_node
from app.application.services.summarization.graph_llm import enrich_two_pass

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("enrich")
async def enrich(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Optional second enrichment pass over the validated summary.

    No-op (byte-identical to the no-enrich path) unless ``two_pass_enabled`` is
    set in the config snapshot. The two-pass call merges only the 8 enrichment
    keys (truthy-only) and never raises -- a failure returns the summary
    unchanged (``enrich_two_pass`` parity).
    """
    config = deps.config if isinstance(deps.config, SummarizeConfig) else None
    if config is None or not config.two_pass_enabled:
        return {}
    summary = state.get("summary")
    if not summary:
        return {}

    enriched = await enrich_two_pass(
        llm_client=deps.llm_client,
        summary=summary,
        content_text=state.get("content_for_summary") or state.get("source_text") or "",
        chosen_lang=state.get("lang") or "en",
        temperature=config.temperature,
        top_p=config.top_p,
        enrichment_max_tokens=config.enrichment_max_tokens,
        correlation_id=state.get("correlation_id"),
    )
    return {"summary": enriched}
