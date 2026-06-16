"""``summarize`` node -- structured summary via the llm_client port (ADR-0006/0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.deps import SummarizeConfig
from app.application.graphs.summarize.nodes._span import graph_node
from app.application.services.summarization.graph_llm import summarize_with_instructor

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("summarize")
async def summarize(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Produce the structured summary via ``deps.llm_client.chat_structured``.

    Runs the ported instructor path (sticky-failure force-fallback, en/ru
    instructor prompt) and records a serializable ``llm_calls`` row (accumulated
    via the state reducer) for the persist node. A failed LLM call raises
    ``ValueError`` -> the single terminal-failure path (ADR-0011). No-ops when
    ``build_prompt`` produced no messages (no extracted content).
    """
    messages = state.get("messages")
    if not messages:
        return {}

    config = deps.config if isinstance(deps.config, SummarizeConfig) else None
    model_override = (state.get("model_override") or "").strip() or None
    max_tokens = state.get("max_tokens") or None

    summary, call_meta = await summarize_with_instructor(
        llm_client=deps.llm_client,
        messages=messages,
        source_content=state.get("content_for_summary") or "",
        max_tokens=max_tokens,
        model_override=model_override,
        temperature=config.temperature if config else 0.2,
        max_retries=config.summarization_max_retries if config else 3,
        sticky_fallback_enabled=config.sticky_fallback_enabled if config else True,
        structured_output_mode=config.structured_output_mode if config else None,
        correlation_id=state.get("correlation_id"),
    )

    return {
        "summary": summary,
        "call_count": state.get("call_count", 0) + 1,
        "llm_calls": [_call_record(state, config, call_meta, status="ok")],
    }


def _call_record(
    state: SummarizeState,
    config: SummarizeConfig | None,
    call_meta: dict[str, Any],
    *,
    status: str,
) -> dict[str, Any]:
    """Build the serializable ``llm_calls`` record (attempt_trigger='graph_node')."""
    return {
        "request_id": state.get("request_id"),
        "provider": "openrouter",
        "model": call_meta.get("model"),
        "tokens_prompt": call_meta.get("tokens_prompt"),
        "tokens_completion": call_meta.get("tokens_completion"),
        "cost_usd": call_meta.get("cost_usd"),
        "latency_ms": call_meta.get("latency_ms"),
        "status": status,
        "structured_output_used": True,
        "structured_output_mode": config.structured_output_mode if config else None,
        "attempt_trigger": "graph_node",
    }
