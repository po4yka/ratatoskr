"""Repair/error-handling mixin for LLM response workflow."""
# mypy: disable-error-code=attr-defined

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.application.services.summarization.llm_response_workflow import AttemptContext
    from app.application.ports.llm_client import LLMClientProtocol

from app.application.services.user_interaction_update import async_safe_update_user_interaction
from app.core.call_status import CallStatus
from app.core.llm_call_budget import LLMCallCapExceeded
from app.core.summary_contract import validate_and_shape_summary
from app.domain.models.request import RequestStatus
from app.utils.json_validation import finalize_summary_texts

logger = logging.getLogger("app.application.services.summarization.llm_response_workflow")


class LLMWorkflowRepairMixin:
    """JSON salvage/repair and failure reporting helpers."""

    # Explicit host contract for composition with LLMResponseWorkflow.
    _audit: Callable[..., None]
    _persist_llm_call: Callable[..., Any]
    _sem: Callable[..., Any]
    _set_failure_context: Callable[..., None]
    cfg: Any
    llm_client: LLMClientProtocol
    request_repo: Any
    user_repo: Any

    def _attempt_salvage_parsing(
        self, llm: Any, correlation_id: str | None
    ) -> dict[str, Any] | None:
        from app.core.json_utils import extract_json

        try:
            parsed = extract_json(llm.response_text or "")
            if isinstance(parsed, dict):
                shaped = validate_and_shape_summary(parsed)
                finalize_summary_texts(shaped)
                if shaped:
                    return shaped

            parse_result = self._get_parse_fn()(llm.response_json, llm.response_text)
            shaped = parse_result.shaped if parse_result else None
            if shaped:
                finalize_summary_texts(shaped)
                logger.info(
                    "structured_output_salvage_success",
                    extra={"cid": correlation_id},
                )
                return shaped
        except Exception as exc:
            logger.exception(
                "salvage_error",
                extra={"error": str(exc), "cid": correlation_id},
            )
        return None

    async def _attempt_json_repair(
        self,
        ctx: AttemptContext,
        *,
        parse_result: Any,
    ) -> dict[str, Any] | None:
        llm = ctx.llm
        req_id = ctx.req_id
        correlation_id = ctx.correlation_id
        interaction_config = ctx.interaction_config
        repair_context = ctx.repair_context
        request_config = ctx.request_config
        notifications = ctx.notifications

        # The repair pass is another provider invocation: charge the shared
        # per-request budget and skip repair cleanly if the cap is exhausted.
        budget = getattr(ctx, "call_budget", None)
        if budget is not None:
            try:
                budget.charge()
            except LLMCallCapExceeded:
                logger.error(
                    "llm_call_cap_reached_repair",
                    extra={
                        "req_id": req_id,
                        "cid": correlation_id,
                        "calls_made": budget.count,
                    },
                )
                self._set_failure_context(llm, "llm_call_cap_reached")
                return None

        try:
            logger.info(
                "json_repair_attempt_enhanced",
                extra={
                    "cid": correlation_id,
                    "reason": (
                        parse_result.errors[-3:] if parse_result and parse_result.errors else None
                    ),
                    "structured_mode": self.cfg.openrouter.structured_output_mode,
                },
            )

            repair_messages = list(repair_context.base_messages)
            repair_messages.append({"role": "assistant", "content": llm.response_text or ""})

            if (
                parse_result
                and parse_result.errors
                and "missing_summary_fields" in parse_result.errors
            ):
                prompt = repair_context.missing_fields_prompt or repair_context.default_prompt
            else:
                prompt = repair_context.default_prompt

            repair_messages.append({"role": "user", "content": prompt})

            sem_timeout = getattr(self.cfg.runtime, "semaphore_acquire_timeout_sec", 30.0)
            llm_timeout = getattr(self.cfg.runtime, "llm_call_timeout_sec", 180.0)

            sem_cm = self._sem()
            try:
                async with asyncio.timeout(sem_timeout):
                    await sem_cm.__aenter__()
            except TimeoutError:
                logger.error(
                    "repair_semaphore_acquire_timeout",
                    extra={
                        "req_id": req_id,
                        "cid": correlation_id,
                        "timeout_sec": sem_timeout,
                    },
                )
                raise

            try:
                logger.debug(
                    "repair_semaphore_acquired",
                    extra={"req_id": req_id, "cid": correlation_id},
                )
                per_model_overrides: dict[str, float] = dict(
                    getattr(self.cfg.runtime, "llm_per_model_timeout_overrides", {}) or {}
                )
                async with asyncio.timeout(llm_timeout):
                    repair = await self.llm_client.chat(
                        repair_messages,
                        temperature=request_config.temperature,
                        max_tokens=repair_context.repair_max_tokens,
                        top_p=request_config.top_p,
                        request_id=req_id,
                        response_format=repair_context.repair_response_format,
                        model_override=request_config.model_override,
                        per_model_timeout_sec=llm_timeout,
                        per_model_timeout_overrides=per_model_overrides or None,
                        budget_tight_ratio=getattr(self.cfg.runtime, "llm_budget_tight_ratio", 0.6),
                        truncation_max_count=getattr(
                            self.cfg.runtime, "llm_truncation_max_count", 2
                        ),
                    )
            except TimeoutError:
                logger.error(
                    "repair_llm_call_timeout",
                    extra={
                        "req_id": req_id,
                        "cid": correlation_id,
                        "llm_timeout_sec": llm_timeout,
                        "model": request_config.model_override,
                    },
                )
                raise
            finally:
                await sem_cm.__aexit__(None, None, None)

            # Persist the repair LLM call so it appears in llm_calls with the
            # correct pathway attribution.  We always persist regardless of
            # success/failure so that failed repair attempts are visible too.
            await self._persist_llm_call(
                repair, req_id, correlation_id, attempt_trigger="repair_loop"
            )

            if repair.status == CallStatus.OK:
                repair_result = self._get_parse_fn()(repair.response_json, repair.response_text)
                if repair_result.shaped is not None:
                    finalize_summary_texts(repair_result.shaped)
                    logger.info(
                        "json_repair_success_enhanced",
                        extra={
                            "cid": correlation_id,
                            "used_local_fix": repair_result.used_local_fix,
                        },
                    )
                    return repair_result.shaped
                msg = "repair_failed"
                raise ValueError(msg)
            msg = "repair_call_error"
            raise ValueError(msg)
        except Exception as exc:
            logger.warning(
                "json_repair_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            await self._handle_repair_failure(
                ctx.message,
                req_id,
                correlation_id,
                interaction_config,
                notifications,
            )
            self._set_failure_context(llm, "json_repair_failed")
            return None

    async def _handle_llm_error(
        self,
        message: Any,
        llm: Any,
        req_id: int,
        correlation_id: str | None,
        interaction_config: Any,
        notifications: Any | None,
        is_final_error: bool = False,
    ) -> None:
        await self.request_repo.async_update_request_status(req_id, RequestStatus.ERROR)

        error_parts: list[str] = []
        context = getattr(llm, "error_context", None) or {}

        status_code = context.get("status_code") if isinstance(context, dict) else None
        if status_code is not None:
            error_parts.append(f"HTTP {status_code}")

        message_text = context.get("message") if isinstance(context, dict) else None
        if message_text:
            error_parts.append(str(message_text))

        api_error = context.get("api_error") if isinstance(context, dict) else None
        if api_error and api_error not in error_parts:
            error_parts.append(str(api_error))

        provider = context.get("provider") if isinstance(context, dict) else None
        if provider:
            error_parts.append(f"Provider: {provider}")

        if llm.error_text and llm.error_text not in error_parts:
            error_parts.append(str(llm.error_text))

        error_details = " | ".join(error_parts) if error_parts else None

        logger.error(
            "openrouter_error",
            extra={"error": llm.error_text, "cid": correlation_id},
        )

        try:
            self._audit(
                "ERROR",
                "openrouter_error",
                {"request_id": req_id, "cid": correlation_id, "error": llm.error_text},
            )
        except Exception as audit_exc:
            logger.warning(
                "audit_log_failed",
                extra={"error": str(audit_exc), "cid": correlation_id},
            )

        if is_final_error and notifications and notifications.llm_error:
            try:
                await notifications.llm_error(llm, error_details)
            except Exception as notif_exc:
                logger.warning(
                    "llm_error_notification_failed",
                    extra={"error": str(notif_exc), "cid": correlation_id},
                )

        if interaction_config.interaction_id and interaction_config.llm_error_builder:
            try:
                kwargs = interaction_config.llm_error_builder(llm, error_details)
                await async_safe_update_user_interaction(
                    self.user_repo,
                    interaction_id=interaction_config.interaction_id,
                    logger_=logger,
                    **kwargs,
                )
            except Exception as exc:
                logger.exception(
                    "interaction_error_update_failed",
                    extra={"cid": correlation_id, "error": str(exc)},
                )

    async def _apply_failure_outcome(
        self,
        req_id: int,
        correlation_id: str | None,
        notification_cb: Any | None,
        notification_label: str,
        interaction_id: Any | None,
        interaction_kwargs: dict[str, Any] | None,
        interaction_label: str,
    ) -> None:
        """Update request status to error, notify, and update interaction record."""
        await self.request_repo.async_update_request_status(req_id, RequestStatus.ERROR)

        if notification_cb:
            try:
                await notification_cb()
            except Exception as notif_exc:
                logger.warning(
                    f"{notification_label}_notification_failed",
                    extra={"error": str(notif_exc), "cid": correlation_id},
                )

        if interaction_id and interaction_kwargs:
            try:
                await async_safe_update_user_interaction(
                    self.user_repo,
                    interaction_id=interaction_id,
                    logger_=logger,
                    **interaction_kwargs,
                )
            except Exception as exc:
                logger.exception(
                    f"interaction_{interaction_label}_update_failed",
                    extra={"cid": correlation_id, "error": str(exc)},
                )

    async def _handle_repair_failure(
        self,
        message: Any,
        req_id: int,
        correlation_id: str | None,
        interaction_config: Any,
        notifications: Any | None,
    ) -> None:
        await self._apply_failure_outcome(
            req_id,
            correlation_id,
            notifications.repair_failure if notifications else None,
            "repair_failure",
            interaction_config.interaction_id,
            interaction_config.repair_failure_kwargs,
            "repair",
        )

    async def _handle_parsing_failure(
        self,
        message: Any,
        req_id: int,
        correlation_id: str | None,
        interaction_config: Any,
        notifications: Any | None,
    ) -> None:
        await self._apply_failure_outcome(
            req_id,
            correlation_id,
            notifications.parsing_failure if notifications else None,
            "parsing_failure",
            interaction_config.interaction_id,
            interaction_config.parsing_failure_kwargs,
            "parsing",
        )

    async def _handle_all_attempts_failed(
        self,
        message: Any,
        req_id: int,
        correlation_id: str | None,
        interaction_config: Any,
        notifications: Any | None,
        failed_attempts: list[tuple[Any, Any]],
    ) -> None:
        """Handle the case when all LLM attempts have failed."""
        await self.request_repo.async_update_request_status(req_id, RequestStatus.ERROR)

        error_details_list: list[str] = []
        models_tried: list[str] = []

        for llm, config in failed_attempts:
            model_name = config.model_override or getattr(llm, "model", None) or "unknown"
            if config.preset_name:
                models_tried.append(f"{model_name}:{config.preset_name}")
            else:
                models_tried.append(model_name)

            context = getattr(llm, "error_context", None) or {}

            error_parts: list[str] = []
            status_code = context.get("status_code") if isinstance(context, dict) else None
            if status_code is not None:
                error_parts.append(f"HTTP {status_code}")

            message_text = context.get("message") if isinstance(context, dict) else None
            if message_text:
                error_parts.append(str(message_text))

            if llm.error_text and llm.error_text not in error_parts:
                error_parts.append(str(llm.error_text))

            if error_parts:
                error_details_list.append(" | ".join(error_parts))

        final_error_details = error_details_list[-1] if error_details_list else None

        comprehensive_details = f"Tried {len(failed_attempts)} model(s): {', '.join(models_tried)}"
        if final_error_details:
            comprehensive_details += f"\nLast error: {final_error_details}"

        logger.error(
            "all_llm_attempts_failed",
            extra={
                "error": final_error_details,
                "cid": correlation_id,
                "models_tried": models_tried,
                "total_attempts": len(failed_attempts),
            },
        )

        try:
            self._audit(
                "ERROR",
                "all_llm_attempts_failed",
                {
                    "request_id": req_id,
                    "cid": correlation_id,
                    "models_tried": models_tried,
                    "error": final_error_details,
                },
            )
        except Exception as audit_exc:
            logger.warning(
                "audit_log_failed",
                extra={"error": str(audit_exc), "cid": correlation_id},
            )

        if notifications and notifications.llm_error:
            try:
                await notifications.llm_error(
                    failed_attempts[-1][0] if failed_attempts else None,
                    comprehensive_details,
                )
            except Exception as notif_exc:
                logger.warning(
                    "llm_error_notification_failed",
                    extra={"error": str(notif_exc), "cid": correlation_id},
                )

        if interaction_config.interaction_id and interaction_config.llm_error_builder:
            try:
                last_llm = failed_attempts[-1][0] if failed_attempts else None
                kwargs = interaction_config.llm_error_builder(last_llm, comprehensive_details)
                await async_safe_update_user_interaction(
                    self.user_repo,
                    interaction_id=interaction_config.interaction_id,
                    logger_=logger,
                    **kwargs,
                )
            except Exception as exc:
                logger.exception(
                    "interaction_error_update_failed",
                    extra={"cid": correlation_id, "error": str(exc)},
                )


__all__ = ["LLMWorkflowRepairMixin"]
