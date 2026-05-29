"""LLM workflow orchestration for attachment summaries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.services.summarization.llm_response_workflow import (
    LLMInteractionConfig,
    LLMRepairContext,
    LLMRequestConfig,
    LLMSummaryPersistenceSettings,
    LLMWorkflowNotifications,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.adapters.attachment._attachment_shared import AttachmentProcessorContext


class AttachmentLLMWorkflowService:
    """Runs the shared summary workflow for image and PDF attachments."""

    def __init__(self, context: AttachmentProcessorContext) -> None:
        self._context = context

    async def run_summary_workflow(
        self,
        *,
        messages: list[dict[str, Any]],
        req_id: int,
        correlation_id: str | None,
        interaction_id: int | None,
        chosen_lang: str,
        message: Any,
        model_override: str | None = None,
        status_updater: Callable[[str], Awaitable[None]] | None = None,
    ) -> dict[str, Any] | None:
        """Execute the standard LLM summary flow for an attachment."""
        max_tokens = 6144
        # Vision models don't reliably support json_schema strict mode; they return
        # 200 OK with non-parseable content instead of rejecting with a 400, so the
        # auto_fallback_structured mechanism never fires. Use json_object for them.
        rf_mode = "json_object" if model_override else None
        response_format = self._context.workflow.build_structured_response_format(mode=rf_mode)

        request_kwargs: dict[str, Any] = {
            "messages": messages,
            "response_format": response_format,
            "max_tokens": max_tokens,
            "temperature": self._context.cfg.openrouter.temperature,
            "top_p": self._context.cfg.openrouter.top_p,
        }
        if model_override:
            request_kwargs["model_override"] = model_override
            attachment_cfg = self._context.cfg.attachment
            if attachment_cfg.vision_fallback_models:
                request_kwargs["fallback_models_override"] = attachment_cfg.vision_fallback_models

        requests = [LLMRequestConfig(**request_kwargs)]
        repair_context = LLMRepairContext(
            base_messages=messages,
            repair_response_format=self._context.workflow.build_structured_response_format(
                mode=rf_mode
            ),
            repair_max_tokens=max_tokens,
            default_prompt=(
                "Your previous message was not a valid JSON object. Respond with ONLY a corrected "
                "JSON that matches the schema exactly."
            ),
        )

        async def _on_completion(llm_result: Any, _: LLMRequestConfig) -> None:
            await self._context.response_formatter.send_forward_completion_notification(
                message,
                llm_result,
            )

        async def _on_llm_error(llm_result: Any, details: str | None) -> None:
            await self._context.response_formatter.send_error_notification(
                message,
                "llm_error",
                correlation_id or "unknown",
                details=details,
            )

        async def _on_processing_failure() -> None:
            await self._context.response_formatter.send_error_notification(
                message,
                "processing_failed",
                correlation_id or "unknown",
            )

        async def _on_retry() -> None:
            if status_updater:
                await status_updater("🧠 <b>AI analysis failed, retrying...</b>")

        notifications = LLMWorkflowNotifications(
            completion=_on_completion,
            llm_error=_on_llm_error,
            repair_failure=_on_processing_failure,
            parsing_failure=_on_processing_failure,
            retry=_on_retry,
        )

        if status_updater:
            model_label = (model_override or self._context.cfg.openrouter.model).split("/")[-1]
            await status_updater(f"🧠 <b>Analyzing with AI ({model_label})...</b>")

        interaction_config = LLMInteractionConfig(
            interaction_id=interaction_id,
            success_kwargs={
                "response_sent": True,
                "response_type": "summary",
                "request_id": req_id,
            },
            llm_error_builder=lambda llm_result, details: {
                "response_sent": True,
                "response_type": "error",
                "error_occurred": True,
                "error_message": details
                or f"LLM error: {llm_result.error_text or 'Unknown error'}",
                "request_id": req_id,
            },
            repair_failure_kwargs={
                "response_sent": True,
                "response_type": "error",
                "error_occurred": True,
                "error_message": "Invalid summary format",
                "request_id": req_id,
            },
            parsing_failure_kwargs={
                "response_sent": True,
                "response_type": "error",
                "error_occurred": True,
                "error_message": "Invalid summary format",
                "request_id": req_id,
            },
        )

        persistence = LLMSummaryPersistenceSettings(
            lang=chosen_lang,
            is_read=True,
        )

        return await self._context.workflow.execute_summary_workflow(
            message=message,
            req_id=req_id,
            correlation_id=correlation_id,
            interaction_config=interaction_config,
            persistence=persistence,
            repair_context=repair_context,
            requests=requests,
            notifications=notifications,
        )
