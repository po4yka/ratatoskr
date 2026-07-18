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

    No-op (byte-identical to the no-enrich path) unless BOTH gates are open:

    - ``state['two_pass_eligible']`` is True -- set ONLY by the URL-flow runners
      (audit #20). The content-only ``summarize`` entrypoint leaves it False, so
      enrichment never runs for pre-extracted callers, matching the legacy
      two-pass scoping (interactive/URL only).
    - ``config.two_pass_enabled`` is set in the config snapshot (default False).

    The two-pass call merges only the 8 enrichment keys (truthy-only) and never
    raises -- a failure returns the summary unchanged (``enrich_two_pass`` parity).

    GAP 3b: records the enrichment LLM call in ``state['llm_calls']`` so the
    persist node writes it (persist-everything rule 3). ``call_meta`` is ``None``
    when ``enrich_two_pass`` raised internally (exception swallowed) -- no row is
    written in that case to avoid double-counting transport-level failures already
    persisted by the llm_client adapter.
    """
    if not state.get("two_pass_eligible"):
        return {}
    config = deps.config if isinstance(deps.config, SummarizeConfig) else None
    if config is None or not config.two_pass_enabled:
        return {}
    summary = state.get("summary")
    if not summary:
        return {}

    enriched, call_metas, call_count = await enrich_two_pass(
        llm_client=deps.llm_client,
        summary=summary,
        content_text=state.get("content_for_summary") or state.get("source_text") or "",
        chosen_lang=state.get("lang") or "en",
        temperature=config.temperature,
        top_p=config.top_p,
        enrichment_max_tokens=config.enrichment_max_tokens,
        enrichment_content_max_chars=config.enrichment_content_max_chars,
        correlation_id=state.get("correlation_id"),
        request_id=state.get("request_id"),
        guard=getattr(deps, "llm_guard", None),
        current_call_count=state.get("call_count", 0),
    )

    result: dict[str, Any] = {"summary": enriched, "call_count": call_count}

    # Append every physical enrichment attempt, including transport failures and
    # provider fallbacks. The adapter's per-model telemetry is already normalized
    # by ``enrich_two_pass``.
    if call_metas:
        provider = getattr(deps.llm_client, "provider_name", None)
        if not isinstance(provider, str) or not provider:
            provider = config.llm_provider
        result["llm_calls"] = [
            {
                "request_id": state.get("request_id"),
                "provider": provider,
                "model": call_meta.get("model"),
                "tokens_prompt": call_meta.get("tokens_prompt"),
                "tokens_completion": call_meta.get("tokens_completion"),
                "cost_usd": call_meta.get("cost_usd"),
                "latency_ms": call_meta.get("latency_ms"),
                "status": call_meta.get("status") or "ok",
                "error_text": call_meta.get("error_text"),
                "structured_output_used": False,
                "structured_output_mode": config.structured_output_mode,
                "attempt_trigger": "graph_node",
            }
            for call_meta in call_metas
        ]

    return result
