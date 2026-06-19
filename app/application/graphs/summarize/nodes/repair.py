"""``repair`` node -- re-prompt to fix contract-validation errors (ADR-0011/0015)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.deps import SummarizeConfig
from app.application.graphs.summarize.lifecycle import CallBudgetExceeded
from app.application.graphs.summarize.nodes._span import graph_node
from app.application.graphs.summarize.state import MAX_REPAIR_ATTEMPTS
from app.application.services.summarization.graph_llm import summarize_with_instructor

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState

logger = logging.getLogger(__name__)


@graph_node("repair")
async def repair(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Re-prompt the LLM to repair contract-validation errors, bounded by budget.

    The validate -> repair -> validate loop is bounded by ``MAX_REPAIR_ATTEMPTS``
    (and, independently, langgraph's per-invocation ``recursion_limit``). When the
    repair budget is exhausted this raises ``CallBudgetExceeded`` -> the single
    terminal-failure path (no parallel error path, ADR-0011).

    Each repair re-runs the structured summarize call (Instructor reasks against
    the same contract); the result is re-checked by the validate node. The repair
    LLM call is recorded into ``llm_calls`` (accumulated via the state reducer).
    A repair LLM failure is swallowed here (budget + validate bound the loop) so a
    transient model error does not short-circuit the remaining repair budget.
    """
    attempts = state.get("repair_attempts", 0) + 1
    if attempts > MAX_REPAIR_ATTEMPTS:
        raise CallBudgetExceeded(f"repair budget exhausted after {MAX_REPAIR_ATTEMPTS} attempts")

    messages = state.get("messages")
    if not messages:
        # Nothing to repair against (no prompt assembled) -- only advance the budget.
        return {"repair_attempts": attempts}

    config = deps.config if isinstance(deps.config, SummarizeConfig) else None
    model_override = (state.get("model_override") or "").strip() or None

    try:
        summary, call_meta = await summarize_with_instructor(
            llm_client=deps.llm_client,
            messages=messages,
            source_content=state.get("content_for_summary") or "",
            max_tokens=state.get("max_tokens") or None,
            model_override=model_override,
            temperature=config.temperature if config else 0.2,
            max_retries=config.summarization_max_retries if config else 3,
            sticky_fallback_enabled=config.sticky_fallback_enabled if config else True,
            structured_output_mode=config.structured_output_mode if config else None,
            correlation_id=state.get("correlation_id"),
        )
    except Exception as exc:
        logger.warning(
            "summarize_graph_repair_failed",
            extra={"cid": state.get("correlation_id"), "attempt": attempts, "error": str(exc)},
        )
        # Persist a failure llm_calls record so the repair attempt is observable
        # in the DB (persist-everything rule 3). Surface real model/latency from
        # __llm_result__ when the adapter attached it; fall back to config model.
        llm_result = getattr(exc, "__llm_result__", None)
        if llm_result is not None:
            raw_model = getattr(llm_result, "model", None) or getattr(
                llm_result, "model_used", None
            )
            failure_meta: dict[str, Any] = {
                "model": raw_model or (config.model if config else None),
                "tokens_prompt": getattr(llm_result, "tokens_prompt", None),
                "tokens_completion": getattr(llm_result, "tokens_completion", None),
                "cost_usd": getattr(llm_result, "cost_usd", None),
                "latency_ms": getattr(llm_result, "latency_ms", None),
            }
        else:
            failure_meta = {
                "model": config.model if config else None,
                "tokens_prompt": None,
                "tokens_completion": None,
                "cost_usd": None,
                "latency_ms": None,
            }
        failure_record: dict[str, Any] = {
            "request_id": state.get("request_id"),
            "provider": "openrouter",
            "model": failure_meta["model"],
            "tokens_prompt": failure_meta["tokens_prompt"],
            "tokens_completion": failure_meta["tokens_completion"],
            "cost_usd": failure_meta["cost_usd"],
            "latency_ms": failure_meta["latency_ms"],
            "status": "error",
            "structured_output_used": True,
            "structured_output_mode": config.structured_output_mode if config else None,
            "attempt_trigger": "graph_node",
            "error_text": str(exc),
        }
        return {"repair_attempts": attempts, "llm_calls": [failure_record]}

    return {
        "repair_attempts": attempts,
        "summary": summary,
        "llm_calls": [
            {
                "request_id": state.get("request_id"),
                "provider": "openrouter",
                "model": call_meta.get("model"),
                "tokens_prompt": call_meta.get("tokens_prompt"),
                "tokens_completion": call_meta.get("tokens_completion"),
                "cost_usd": call_meta.get("cost_usd"),
                "latency_ms": call_meta.get("latency_ms"),
                "status": "ok",
                "structured_output_used": True,
                "structured_output_mode": config.structured_output_mode if config else None,
                "attempt_trigger": "graph_node",
            }
        ],
    }
