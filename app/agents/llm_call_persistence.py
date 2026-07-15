"""Shared best-effort persistence contract for agent-originated LLM calls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.observability.metrics import record_llm_call_persisted

if TYPE_CHECKING:
    from app.application.ports.requests import LLMCallRecord, LLMRepositoryPort

logger = get_logger(__name__)


def _provider_from_model(model: str | None) -> str:
    if not model:
        return "unknown"
    return model.split("/", 1)[0] if "/" in model else "unknown"


async def persist_agent_llm_call(
    llm_repo: LLMRepositoryPort | None,
    *,
    request_id: int | None,
    endpoint: str,
    model: str | None,
    status: str,
    result: Any = None,
    latency_ms: int | None = None,
    error: Exception | None = None,
    response_text: str | None = None,
    tokens_prompt: int | None = None,
    tokens_completion: int | None = None,
    cost_usd: float | None = None,
    attempt_index: int | None = None,
    attempt_trigger: str | None = None,
    correlation_id: str | None = None,
    structured_output_used: bool | None = None,
) -> None:
    """Write one normalized agent LLM-call record without changing agent outcomes.

    Agent calls are not guaranteed to have a parent request (for example, an
    MCP aggregation can be assembled from external sources), so ``request_id``
    is intentionally nullable. Persistence failures are observable but never
    turn an otherwise valid agent result into a failure.
    """
    if llm_repo is None:
        return

    resolved_model = str(getattr(result, "model_used", None) or model or "unknown")
    payload: LLMCallRecord = {
        "request_id": request_id,
        "provider": _provider_from_model(resolved_model),
        "model": resolved_model,
        "endpoint": endpoint,
        "response_text": response_text,
        "tokens_prompt": tokens_prompt
        if tokens_prompt is not None
        else getattr(result, "tokens_prompt", None),
        "tokens_completion": (
            tokens_completion
            if tokens_completion is not None
            else getattr(result, "tokens_completion", None)
        ),
        "cost_usd": cost_usd if cost_usd is not None else getattr(result, "cost_usd", None),
        "latency_ms": latency_ms if latency_ms is not None else getattr(result, "latency_ms", None),
        "status": status,
        "structured_output_used": (
            result is not None if structured_output_used is None else structured_output_used
        ),
        "structured_output_mode": getattr(result, "structured_output_mode", None),
        "attempt_index": attempt_index,
        "attempt_trigger": attempt_trigger,
    }
    if error is not None:
        payload["error_text"] = str(error)[:2000]

    try:
        await llm_repo.async_insert_llm_call(payload)
        record_llm_call_persisted(dict(payload))
    except Exception as exc:
        raise_if_cancelled(exc)
        logger.warning(
            "agent_llm_call_persist_failed",
            extra={"endpoint": endpoint, "correlation_id": correlation_id, "error": str(exc)},
        )
