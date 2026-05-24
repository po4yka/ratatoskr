"""Execution/lifecycle mixin for LLM response workflow."""
# mypy: disable-error-code=attr-defined

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

from app.core.async_utils import raise_if_cancelled
from app.core.call_status import CallStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from app.adapters.llm.protocol import LLMClientProtocol

logger = logging.getLogger("app.adapters.content.llm_response_workflow")


class LLMWorkflowExecutionMixin:
    """Workflow execution, retries, and lifecycle helpers."""

    # Explicit host contract for composition with LLMResponseWorkflow.
    _adaptive_timeout: Any
    _background_tasks: set[asyncio.Task[Any]]
    _handle_all_attempts_failed: Callable[..., Any]
    _persist_llm_call: Callable[..., Any]
    _evaluate_attempt_outcome: Callable[..., Any]
    _sem: Callable[..., Any]
    _set_failure_context: Callable[..., None]
    cfg: Any
    llm_repo: Any
    llm_client: LLMClientProtocol

    def _schedule_background_task(
        self, coro: Coroutine[Any, Any, Any], label: str, correlation_id: str | None
    ) -> asyncio.Task[Any] | None:
        """Run a persistence task in the background and log errors."""
        try:
            task: asyncio.Task[Any] = asyncio.create_task(coro)
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except RuntimeError as exc:
            logger.error(
                "background_task_schedule_failed",
                extra={"label": label, "cid": correlation_id, "error": str(exc)},
            )
            return None

        def _log_task_error(t: asyncio.Task[Any]) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.error(
                    "background_task_failed",
                    extra={"label": label, "cid": correlation_id, "error": str(exc)},
                )

        task.add_done_callback(_log_task_error)
        return task

    async def aclose(self, timeout: float = 5.0) -> None:
        """Wait for all background tasks to complete."""
        if not self._background_tasks:
            return

        tasks = list(self._background_tasks)
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            logger.warning(
                "llm_workflow_shutdown_timeout",
                extra={"pending": len(self._background_tasks)},
            )
        except Exception as e:
            raise_if_cancelled(e)
            logger.error("llm_workflow_shutdown_error", extra={"error": str(e)})

    async def execute_summary_workflow(
        self,
        *,
        message: Any,
        req_id: int,
        correlation_id: str | None,
        interaction_config: Any,
        persistence: Any,
        repair_context: Any,
        requests: Sequence[Any],
        notifications: Any | None = None,
        ensure_summary: Callable[[dict[str, Any]], Any] | None = None,
        on_attempt: Callable[[Any], Any] | None = None,
        on_success: Callable[[dict[str, Any], Any], Any] | None = None,
        required_summary_fields: Sequence[str] = (
            "tldr",
            "summary_250",
            "summary_1000",
        ),
        defer_persistence: bool = False,
    ) -> dict[str, Any] | None:
        """Run the shared summary processing workflow for a sequence of attempts."""
        if not requests:
            msg = "requests must include at least one attempt"
            raise ValueError(msg)

        from app.adapters.content.llm_response_workflow import AttemptContext

        failed_attempts: list[tuple[Any, Any]] = []
        total_attempts = len(requests)

        for attempt_index, attempt in enumerate(requests):
            is_last_attempt = attempt_index == total_attempts - 1

            on_retry = notifications.retry if notifications else None
            try:
                llm = await self._invoke_llm(attempt, req_id, on_retry=on_retry)
            except TimeoutError:
                logger.error(
                    "llm_invoke_timeout_skipping_attempt",
                    extra={
                        "req_id": req_id,
                        "cid": correlation_id,
                        "attempt_index": attempt_index,
                        "preset": attempt.preset_name,
                        "model": attempt.model_override,
                    },
                )
                from app.adapter_models.llm.llm_models import LLMCallResult

                llm = LLMCallResult(
                    status=CallStatus.ERROR,
                    model=attempt.model_override,
                    error_text=f"LLM call timed out for model {attempt.model_override}",
                    error_context={"message": "timeout", "timeout": True},
                )

            if on_attempt is not None:
                await on_attempt(llm)

            if defer_persistence or persistence.defer_write:
                self._schedule_background_task(
                    self._persist_llm_call(llm, req_id, correlation_id),
                    "persist_llm_call",
                    correlation_id,
                )
            else:
                await self._persist_llm_call(llm, req_id, correlation_id)

            if (
                notifications
                and notifications.completion
                and (llm.status == CallStatus.OK or is_last_attempt)
            ):
                await notifications.completion(llm, attempt)

            summary = None
            try:
                attempt_ctx = AttemptContext(
                    message=message,
                    llm=llm,
                    req_id=req_id,
                    correlation_id=correlation_id,
                    interaction_config=interaction_config,
                    persistence=persistence,
                    repair_context=repair_context,
                    request_config=attempt,
                    notifications=notifications,
                    ensure_summary=ensure_summary,
                    on_success=on_success,
                    required_summary_fields=tuple(required_summary_fields),
                    is_last_attempt=is_last_attempt,
                    failed_attempts=failed_attempts,
                    defer_persistence=defer_persistence,
                )
                summary = await self._evaluate_attempt_outcome(attempt_ctx)
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception(
                    "summary_attempt_processing_failed",
                    extra={
                        "cid": correlation_id,
                        "preset": attempt.preset_name,
                        "model": attempt.model_override,
                        "error": str(exc),
                    },
                )
                self._set_failure_context(llm, "summary_processing_exception")
                context = getattr(llm, "error_context", None) or {}
                context.setdefault("message", "summary_processing_exception")
                context.setdefault("exception", str(exc))
                llm.error_context = context

            if summary is not None:
                return cast("dict[str, Any] | None", summary)

            failed_attempts.append((llm, attempt))

        await self._handle_all_attempts_failed(
            message,
            req_id,
            correlation_id,
            interaction_config,
            notifications,
            failed_attempts,
        )
        return None

    def build_structured_response_format(self, mode: str | None = None) -> dict[str, Any]:
        """Build response format configuration for structured outputs."""
        try:
            from app.core.summary_contract import get_summary_contract_descriptor

            current_mode = mode or self.cfg.openrouter.structured_output_mode
            return get_summary_contract_descriptor().response_format(current_mode)
        except (AttributeError, ValueError, RuntimeError):
            return {"type": "json_object"}

    async def persist_llm_call(
        self,
        llm: Any,
        req_id: int,
        correlation_id: str | None,
        attempt_trigger: str | None = None,
    ) -> None:
        """Public helper to persist an LLM call."""
        await self._persist_llm_call(llm, req_id, correlation_id, attempt_trigger=attempt_trigger)

    async def _resolve_llm_timeout(self, model: str | None) -> tuple[float, str]:
        """Determine the LLM call timeout, preferring the adaptive service."""
        fixed_timeout = float(getattr(self.cfg.runtime, "llm_call_timeout_sec", 180.0))

        if self._adaptive_timeout is not None:
            try:
                adaptive_val = await self._adaptive_timeout.get_llm_timeout(model=model)
                if adaptive_val and adaptive_val > 0:
                    return float(adaptive_val), "adaptive"
            except Exception as exc:
                logger.warning(
                    "adaptive_timeout_lookup_failed",
                    extra={"model": model, "error": str(exc)},
                )

        return fixed_timeout, "fixed"

    async def _invoke_llm(self, request: Any, req_id: int, on_retry: Any | None = None) -> Any:
        from app.adapter_models.llm.llm_models import LLMCallResult
        from app.adapters.content.llm_response_workflow import ConcurrencyTimeoutError
        from app.adapters.llm.usage_budget import (
            LLMUsageSnapshot,
            day_start,
            evaluate_aggregate_budget,
            month_start,
        )

        sem_timeout = getattr(self.cfg.runtime, "semaphore_acquire_timeout_sec", 30.0)
        llm_timeout, timeout_source = await self._resolve_llm_timeout(request.model_override)
        request_max_tokens = request.max_tokens
        budget = getattr(self.cfg, "llm_usage_budget", None)
        if budget is not None:
            configured_max_tokens = getattr(budget, "max_tokens_per_request", None)
            if configured_max_tokens is not None:
                configured_max_tokens = int(configured_max_tokens)
                request_max_tokens = (
                    configured_max_tokens
                    if request_max_tokens is None
                    else min(int(request_max_tokens), configured_max_tokens)
                )
            try:
                usage = LLMUsageSnapshot(
                    daily_cost_usd=await self.llm_repo.async_get_cost_usd_since(day_start()),
                    monthly_cost_usd=await self.llm_repo.async_get_cost_usd_since(month_start()),
                )
                decision = evaluate_aggregate_budget(budget=budget, usage=usage)
            except Exception as exc:
                logger.warning(
                    "llm_budget_lookup_failed", extra={"req_id": req_id, "error": str(exc)}
                )
            else:
                if decision.warning:
                    logger.warning(
                        "llm_budget_warning",
                        extra={
                            "req_id": req_id,
                            "status": decision.status,
                            "reasons": list(decision.reasons),
                        },
                    )
                if not decision.allowed:
                    logger.error(
                        "llm_budget_hard_stop",
                        extra={
                            "req_id": req_id,
                            "status": decision.status,
                            "reasons": list(decision.reasons),
                        },
                    )
                    return LLMCallResult(
                        status=CallStatus.ERROR,
                        model=request.model_override or getattr(self.cfg.openrouter, "model", None),
                        response_text=None,
                        error_text="LLM usage budget hard stop",
                        tokens_prompt=0,
                        tokens_completion=0,
                        cost_usd=0.0,
                        latency_ms=0,
                        error_context={
                            "message": "llm_budget_hard_stop",
                            "usage_budget_status": decision.status,
                            "usage_budget_reasons": list(decision.reasons),
                        },
                    )

        # Compute per-model timeout: divide total budget among models in fallback chain,
        # then enforce a minimum floor so slow models in long ladders are not starved.
        # The floor (LLM_PER_MODEL_TIMEOUT_MIN_SEC, default 90s) can push the worst-case
        # total runtime past llm_timeout when every model fails — that is the intended
        # trade-off: a coherent answer from one slow model beats a guaranteed-fast
        # cascade of timeouts.
        #
        # effective_llm_timeout expands the outer asyncio.timeout() wrapper to at least
        # fit the full cascade (num_models * per_model_timeout + 15s inter-model buffer).
        # The 15s buffer covers semaphore/network overhead when fallback fires.
        # No buffer when num_models == 1 (single-shot call, full testability).
        fallback_models = request.fallback_models_override or getattr(
            self.llm_client, "_fallback_models", ()
        )
        num_models = 1 + len(fallback_models or ())
        per_model_min = float(getattr(self.cfg.runtime, "llm_per_model_timeout_min_sec", 90.0))
        per_model_timeout = max(per_model_min, llm_timeout / max(num_models, 1))
        between_model_buffer = 15.0 if num_models > 1 else 0.0
        effective_llm_timeout = max(
            llm_timeout, num_models * per_model_timeout + between_model_buffer
        )

        per_model_overrides: dict[str, float] = dict(
            getattr(self.cfg.runtime, "llm_per_model_timeout_overrides", {}) or {}
        )

        logger.debug(
            "llm_timeout_resolved",
            extra={
                "req_id": req_id,
                "model": request.model_override,
                "llm_timeout_sec": llm_timeout,
                "effective_llm_timeout_sec": effective_llm_timeout,
                "per_model_timeout_sec": per_model_timeout,
                "per_model_min_sec": per_model_min,
                "num_models": num_models,
                "timeout_source": timeout_source,
                "per_model_overrides_keys": sorted(per_model_overrides.keys()),
            },
        )

        sem_cm = self._sem()
        try:
            async with asyncio.timeout(sem_timeout):
                await sem_cm.__aenter__()
        except TimeoutError:
            logger.error(
                "llm_semaphore_acquire_timeout",
                extra={"req_id": req_id, "timeout_sec": sem_timeout},
            )
            msg = f"Failed to acquire processing slot within {sem_timeout}s"
            raise ConcurrencyTimeoutError(msg) from None

        try:
            logger.debug(
                "llm_semaphore_acquired",
                extra={"req_id": req_id, "model": request.model_override},
            )
            async with asyncio.timeout(effective_llm_timeout):
                return await self.llm_client.chat(
                    request.messages,
                    temperature=request.temperature,
                    max_tokens=request_max_tokens,
                    top_p=request.top_p,
                    stream=bool(getattr(request, "stream", False)),
                    request_id=req_id,
                    response_format=request.response_format,
                    model_override=request.model_override,
                    fallback_models_override=request.fallback_models_override,
                    on_stream_delta=getattr(request, "on_stream_delta", None),
                    per_model_timeout_sec=per_model_timeout,
                    per_model_timeout_overrides=per_model_overrides or None,
                    budget_tight_ratio=getattr(self.cfg.runtime, "llm_budget_tight_ratio", 0.6),
                    truncation_max_count=getattr(self.cfg.runtime, "llm_truncation_max_count", 2),
                )
        except TimeoutError:
            logger.error(
                "llm_call_timeout",
                extra={
                    "req_id": req_id,
                    "llm_timeout_sec": llm_timeout,
                    "effective_llm_timeout_sec": effective_llm_timeout,
                    "per_model_timeout_sec": per_model_timeout,
                    "timeout_source": timeout_source,
                    "model": request.model_override,
                },
            )
            raise
        finally:
            await sem_cm.__aexit__(None, None, None)
