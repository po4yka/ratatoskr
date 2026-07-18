"""``repair`` node -- re-prompt to fix contract-validation errors (ADR-0011/0015)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.deps import SummarizeConfig
from app.application.graphs.summarize.lifecycle import CallBudgetExceeded
from app.application.graphs.summarize.nodes._span import graph_node
from app.application.graphs.summarize.state import MAX_REPAIR_ATTEMPTS
from app.application.services.summarization.graph_llm import summarize_with_instructor
from app.application.services.summarization.graph_llm_guard import (
    GraphLLMUsageBudgetExceeded,
)
from app.core.json_utils import dumps as json_dumps
from app.core.llm_call_budget import LLMCallCapExceeded

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState

logger = logging.getLogger(__name__)

# Mirrors the legacy ``LLMRepairContext.default_prompt`` phrasing
# (app/adapters/content/summary_request_factory.py) so the corrective
# instruction reads the same across both the graph and legacy paths.
_CORRECTION_PREFIX = (
    "Your previous response did not satisfy the required schema. Fix exactly "
    "the following issues while keeping everything else unchanged, then "
    "respond with ONLY the corrected JSON object that matches the schema "
    "exactly:"
)


def _build_repair_messages(
    messages: list[dict[str, Any]],
    *,
    prior_summary: dict[str, Any] | None,
    validation_errors: list[str],
) -> list[dict[str, Any]]:
    """Augment the original prompt with the prior candidate + targeted feedback.

    Mirrors the legacy ``_attempt_json_repair`` shape: original messages + an
    assistant turn carrying the prior (invalid) candidate + a user turn
    enumerating exactly what was wrong, so the model self-corrects instead of
    blindly re-answering the original prompt (ADR-0011/0015 follow-up).
    """
    repair_messages = list(messages)
    if prior_summary:
        repair_messages.append({"role": "assistant", "content": json_dumps(prior_summary)})
    if validation_errors:
        issues = "\n".join(f"- {error}" for error in validation_errors)
        correction = f"{_CORRECTION_PREFIX}\n{issues}"
    else:
        correction = _CORRECTION_PREFIX
    repair_messages.append({"role": "user", "content": correction})
    return repair_messages


@graph_node("repair")
async def repair(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Re-prompt the LLM to repair contract-validation errors, bounded by budget.

    The validate -> repair -> validate loop is bounded by ``MAX_REPAIR_ATTEMPTS``
    (and, independently, langgraph's per-invocation ``recursion_limit``). When the
    repair budget is exhausted this raises ``CallBudgetExceeded`` -> the single
    terminal-failure path (no parallel error path, ADR-0011).

    Each repair re-runs the structured summarize call (Instructor reasks against
    the same contract) against an *augmented* prompt: the original messages plus
    an assistant turn carrying the prior (invalid) candidate from
    ``state['summary']`` plus a user turn enumerating ``state['validation_errors']``
    so the model targets exactly what was wrong instead of blindly re-answering
    the original prompt (mirrors the legacy ``_attempt_json_repair``). The result
    is re-checked by the validate node. The repair LLM call is recorded into
    ``llm_calls`` (accumulated via the state reducer). A repair LLM failure is
    swallowed here (budget + validate bound the loop) so a transient model error
    does not short-circuit the remaining repair budget.
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
    repair_messages = _build_repair_messages(
        messages,
        prior_summary=state.get("summary"),
        validation_errors=state.get("validation_errors") or [],
    )

    try:
        summary, call_metas, call_count = await summarize_with_instructor(
            llm_client=deps.llm_client,
            messages=repair_messages,
            source_content=state.get("content_for_summary") or "",
            max_tokens=state.get("max_tokens") or None,
            model_override=model_override,
            temperature=config.temperature if config else 0.2,
            max_retries=config.summarization_max_retries if config else 3,
            sticky_fallback_enabled=config.sticky_fallback_enabled if config else True,
            structured_output_mode=config.structured_output_mode if config else None,
            correlation_id=state.get("correlation_id"),
            request_id=state.get("request_id"),
            guard=getattr(deps, "llm_guard", None),
            current_call_count=state.get("call_count", 0),
        )
    except (LLMCallCapExceeded, GraphLLMUsageBudgetExceeded) as exc:
        raise CallBudgetExceeded(str(exc)) from exc
    except Exception as exc:
        logger.warning(
            "summarize_graph_repair_failed",
            extra={"cid": state.get("correlation_id"), "attempt": attempts, "error": str(exc)},
        )
        physical_attempts = getattr(exc, "__llm_physical_attempts__", None)
        if isinstance(physical_attempts, list) and physical_attempts:
            failure_records = [
                _repair_call_record(
                    state,
                    config,
                    attempt,
                    status=str(attempt.get("status") or "error"),
                    error_text=str(attempt.get("error_text") or exc),
                )
                for attempt in physical_attempts
                if isinstance(attempt, dict)
            ]
            return {
                "repair_attempts": attempts,
                "call_count": int(getattr(exc, "__llm_call_count__", state.get("call_count", 0))),
                "llm_calls": failure_records,
            }

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
        failure_record = _repair_call_record(
            state, config, failure_meta, status="error", error_text=str(exc)
        )
        return {
            "repair_attempts": attempts,
            "call_count": int(getattr(exc, "__llm_call_count__", state.get("call_count", 0))),
            "llm_calls": [failure_record],
        }

    return {
        "repair_attempts": attempts,
        "call_count": call_count,
        "summary": summary,
        "llm_calls": [
            _repair_call_record(
                state,
                config,
                call_meta,
                status=str(call_meta.get("status") or "ok"),
                error_text=(str(call_meta["error_text"]) if call_meta.get("error_text") else None),
            )
            for call_meta in call_metas
        ],
    }


def _repair_call_record(
    state: SummarizeState,
    config: SummarizeConfig | None,
    call_meta: dict[str, Any],
    *,
    status: str,
    error_text: str | None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "request_id": state.get("request_id"),
        "provider": "openrouter",
        "model": call_meta.get("model"),
        "tokens_prompt": call_meta.get("tokens_prompt"),
        "tokens_completion": call_meta.get("tokens_completion"),
        "cost_usd": call_meta.get("cost_usd"),
        "latency_ms": call_meta.get("latency_ms"),
        "fallback_model_used": _fallback_model(config, call_meta),
        "status": status,
        "structured_output_used": True,
        "structured_output_mode": config.structured_output_mode if config else None,
        "attempt_trigger": "repair_loop",
    }
    if error_text:
        record["error_text"] = error_text
    return record


def _fallback_model(config: SummarizeConfig | None, call_meta: dict[str, Any]) -> str | None:
    model = call_meta.get("model")
    if not isinstance(model, str) or not model:
        return None
    return model if config is not None and model != config.model else None
