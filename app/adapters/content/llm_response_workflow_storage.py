"""Storage persistence mixin for LLM response workflow."""
# mypy: disable-error-code=attr-defined

from __future__ import annotations

import logging
from typing import Any

from app.core.llm_usage_budget import evaluate_request_usage
from app.observability.metrics import record_llm_call_persisted

logger = logging.getLogger("app.adapters.content.llm_response_workflow")


class LLMWorkflowStorageMixin:
    """Persistence helpers for raw LLM calls."""

    # Explicit host contract for composition with LLMResponseWorkflow.
    _db_write_queue: Any
    cfg: Any
    llm_repo: Any

    def _build_llm_call_payload(
        self,
        llm: Any,
        req_id: int,
        attempt_trigger: str | None = None,
    ) -> dict[str, Any]:
        """Serialize an LLM call once so queue batching can reuse the payload."""
        error_context = (
            dict(getattr(llm, "error_context", {}))
            if isinstance(getattr(llm, "error_context", None), dict)
            else (
                getattr(llm, "error_context", None)
                if getattr(llm, "error_context", None) is not None
                else None
            )
        )
        budget = getattr(self.cfg, "llm_usage_budget", None)
        if budget is not None:
            decision = evaluate_request_usage(
                budget=budget,
                prompt_tokens=getattr(llm, "tokens_prompt", None),
                completion_tokens=getattr(llm, "tokens_completion", None),
                cost_usd=getattr(llm, "cost_usd", None),
            )
            if decision.reasons:
                if not isinstance(error_context, dict):
                    error_context = {}
                error_context["usage_budget_status"] = decision.status
                error_context["usage_budget_reasons"] = list(decision.reasons)

        payload: dict[str, Any] = {
            "request_id": req_id,
            "provider": "openrouter",
            "model": llm.model or self.cfg.openrouter.model,
            "endpoint": llm.endpoint,
            "request_headers_json": llm.request_headers or {},
            "request_messages_json": list(llm.request_messages or []),
            "response_text": llm.response_text,
            "response_json": llm.response_json or {},
            "tokens_prompt": llm.tokens_prompt,
            "tokens_completion": llm.tokens_completion,
            "cost_usd": llm.cost_usd,
            "latency_ms": llm.latency_ms,
            "status": getattr(llm.status, "value", llm.status),
            "error_text": llm.error_text,
            "structured_output_used": getattr(llm, "structured_output_used", None),
            "structured_output_mode": getattr(llm, "structured_output_mode", None),
            "error_context_json": error_context,
        }
        if attempt_trigger is not None:
            payload["attempt_trigger"] = attempt_trigger
        if not self._should_persist_llm_prompt_response_payloads():
            _strip_llm_prompt_response_payloads(payload)
        return payload

    async def _persist_llm_calls_batch(self, calls: list[dict[str, Any]]) -> None:
        """Persist multiple LLM calls together when the queue can coalesce them."""
        try:
            await self.llm_repo.async_insert_llm_calls_batch(calls)
            for call in calls:
                record_llm_call_persisted(call)
        except Exception as exc:
            logger.exception(
                "persist_llm_batch_error",
                extra={"error": str(exc), "count": len(calls)},
            )

    def _build_cascade_attempt_payload(
        self,
        llm: Any,
        req_id: int,
        attempt: dict[str, Any],
    ) -> dict[str, Any]:
        """Build a persistence payload for one non-terminal cascade attempt."""
        model = (
            attempt.get("model")
            or getattr(llm, "model", None)
            or (getattr(self.cfg, "openrouter", None) and self.cfg.openrouter.model)
            or "unknown"
        )
        payload = {
            "request_id": req_id,
            "provider": "openrouter",
            "model": model,
            "endpoint": getattr(llm, "endpoint", "/api/v1/chat/completions"),
            "request_headers_json": {},
            "request_messages_json": [],
            "response_text": None,
            "response_json": {},
            "tokens_prompt": None,
            "tokens_completion": None,
            "cost_usd": None,
            "latency_ms": attempt.get("latency_ms"),
            "status": "error",
            "error_text": attempt.get("error_text"),
            "structured_output_used": None,
            "structured_output_mode": None,
            "error_context_json": attempt.get("error_context") or None,
            "attempt_trigger": "auto_backfill",
        }
        if not self._should_persist_llm_prompt_response_payloads():
            _strip_llm_prompt_response_payloads(payload)
        return payload

    def _should_persist_llm_prompt_response_payloads(self) -> bool:
        retention = getattr(self.cfg, "retention", None)
        if retention is None:
            return True
        return bool(getattr(retention, "persist_llm_prompt_response_payloads", True))

    async def _persist_llm_call(
        self,
        llm: Any,
        req_id: int,
        correlation_id: str | None,
        attempt_trigger: str | None = None,
    ) -> None:
        cascade_attempts = list(getattr(llm, "per_model_attempts", []) or [])
        payload = self._build_llm_call_payload(llm, req_id, attempt_trigger=attempt_trigger)

        if self._db_write_queue is not None:
            for cascade_attempt in cascade_attempts:
                try:
                    cascade_payload = self._build_cascade_attempt_payload(
                        llm, req_id, cascade_attempt
                    )
                    await self._db_write_queue.enqueue_batch(
                        cascade_payload,
                        batch_key=f"persist_llm_call:{id(self.llm_repo)}",
                        execute_batch=self._persist_llm_calls_batch,
                        operation_name="persist_llm_call_cascade",
                        correlation_id=correlation_id or "",
                    )
                except Exception as exc:
                    logger.warning(
                        "persist_llm_cascade_error",
                        extra={"error": str(exc), "cid": correlation_id},
                    )
            await self._db_write_queue.enqueue_batch(
                payload,
                batch_key=f"persist_llm_call:{id(self.llm_repo)}",
                execute_batch=self._persist_llm_calls_batch,
                operation_name="persist_llm_call",
                correlation_id=correlation_id or "",
            )
            return

        for cascade_attempt in cascade_attempts:
            try:
                cascade_payload = self._build_cascade_attempt_payload(llm, req_id, cascade_attempt)
                await self.llm_repo.async_insert_llm_call(cascade_payload)
                record_llm_call_persisted(cascade_payload)
            except Exception as exc:
                logger.warning(
                    "persist_llm_cascade_error",
                    extra={"error": str(exc), "cid": correlation_id},
                )

        try:
            await self.llm_repo.async_insert_llm_call(payload)
            record_llm_call_persisted(payload)
        except Exception as exc:
            logger.exception(
                "persist_llm_error",
                extra={"error": str(exc), "cid": correlation_id},
            )


def _strip_llm_prompt_response_payloads(payload: dict[str, Any]) -> None:
    payload["request_headers_json"] = {}
    payload["request_messages_json"] = []
    payload["response_text"] = None
    payload["response_json"] = {}
    payload["openrouter_response_text"] = None
    payload["openrouter_response_json"] = {}


__all__ = ["LLMWorkflowStorageMixin", "_strip_llm_prompt_response_payloads"]
