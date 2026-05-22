"""Forward message summarization logic."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.content.llm_response_workflow import (
    LLMInteractionConfig,
    LLMRepairContext,
    LLMRequestConfig,
    LLMResponseWorkflow,
    LLMSummaryPersistenceSettings,
    LLMWorkflowNotifications,
)
from app.adapters.content.summary_request_factory import (
    build_summary_user_prompt,
    mark_prompt_injection_metadata,
)
from app.core.summary_contract_impl.quality_metadata import merge_summary_quality_metadata
from app.core.logging_utils import get_logger
from app.utils.typing_indicator import typing_indicator

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.llm.protocol import LLMClientProtocol
    from app.application.ports.requests import LLMRepositoryPort, RequestRepositoryPort
    from app.application.ports.summaries import SummaryRepositoryPort
    from app.application.ports.users import UserRepositoryPort
    from app.config import AppConfig
    from app.db.session import Database
    from app.db.write_queue import DbWriteQueue

logger = get_logger(__name__)

# Maximum character length for forward content sent to the LLM.
# Typical model context windows are ~128k tokens; 45k chars (~11k tokens) leaves
# ample room for the system prompt, response format schema, and generated output.
_MAX_FORWARD_CONTENT_CHARS = 45_000


class ForwardSummarizer:
    """Handles AI summarization for forwarded messages."""

    def __init__(
        self,
        cfg: AppConfig,
        db: Database,
        openrouter: LLMClientProtocol,
        response_formatter: ResponseFormatter,
        audit_func: Callable[[str, str, dict], None],
        sem: Callable[[], Any],
        db_write_queue: DbWriteQueue | None = None,
        *,
        summary_repo: SummaryRepositoryPort | None = None,
        request_repo: RequestRepositoryPort | None = None,
        llm_repo: LLMRepositoryPort | None = None,
        user_repo: UserRepositoryPort | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.openrouter = openrouter
        self.response_formatter = response_formatter
        self._audit = audit_func
        self.sem = sem
        self._workflow = LLMResponseWorkflow(
            cfg=cfg,
            db=db,
            openrouter=openrouter,
            response_formatter=response_formatter,
            audit_func=audit_func,
            sem=sem,
            db_write_queue=db_write_queue,
            summary_repo=summary_repo,
            request_repo=request_repo,
            llm_repo=llm_repo,
            user_repo=user_repo,
        )

    async def summarize_forward(
        self,
        message: Any,
        prompt: str,
        chosen_lang: str,
        system_prompt: str,
        req_id: int,
        correlation_id: str | None = None,
        interaction_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Summarize forwarded message content."""
        # Truncate content if too long
        if len(prompt) > _MAX_FORWARD_CONTENT_CHARS:
            original_length = len(prompt)
            prompt = prompt[:_MAX_FORWARD_CONTENT_CHARS] + "\n\n[Content truncated due to length]"
            logger.warning(
                "content_truncated",
                extra={
                    "original_length": original_length,
                    "truncated_length": _MAX_FORWARD_CONTENT_CHARS,
                    "cid": correlation_id,
                },
            )

        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": build_summary_user_prompt(
                    content_for_summary=prompt,
                    chosen_lang=chosen_lang,
                ),
            },
        ]

        # ``max_tokens`` is the OUTPUT budget, not total -- the structured
        # summary JSON schema (35+ fields, nested structures) needs ~6k output
        # tokens regardless of input size. Sizing this from ``len(prompt)``
        # used to give short forwards only ~2-3k tokens, the LLM truncated
        # mid-JSON, the budget-tight guard then skipped truncation recovery,
        # and every fallback model died with
        # ``truncation_recovery_skipped_budget_tight``.
        forward_tokens = 6144

        response_format = self._workflow.build_structured_response_format()
        requests = [
            LLMRequestConfig(
                messages=messages,
                response_format=response_format,
                max_tokens=forward_tokens,
                temperature=self.cfg.openrouter.temperature,
                top_p=self.cfg.openrouter.top_p,
            )
        ]
        stream_coordinator = None
        if self._summary_streaming_enabled():
            from app.adapters.telegram.summary_draft_streaming import SummaryDraftStreamCoordinator

            stream_coordinator = SummaryDraftStreamCoordinator(
                response_formatter=self.response_formatter,
                message=message,
                correlation_id=correlation_id,
                request_id=str(req_id),
            )
            for request in requests:
                request.stream = True
                request.on_stream_delta = stream_coordinator.on_delta

        repair_context = LLMRepairContext(
            base_messages=messages,
            repair_response_format=self._workflow.build_structured_response_format(),
            repair_max_tokens=forward_tokens,
            default_prompt=(
                "Your previous message was not a valid JSON object. Respond with ONLY a corrected JSON "
                "that matches the schema exactly."
            ),
        )

        async def _on_completion(llm_result: Any, _: LLMRequestConfig) -> None:
            await self.response_formatter.send_forward_completion_notification(message, llm_result)

        async def _on_llm_error(llm_result: Any, details: str | None) -> None:
            await self.response_formatter.send_error_notification(
                message,
                "llm_error",
                correlation_id or "unknown",
                details=details,
            )

        async def _on_processing_failure() -> None:
            await self.response_formatter.send_error_notification(
                message,
                "processing_failed",
                correlation_id or "unknown",
            )

        notifications = LLMWorkflowNotifications(
            completion=_on_completion,
            llm_error=_on_llm_error,
            repair_failure=_on_processing_failure,
            parsing_failure=_on_processing_failure,
        )

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

        # Forwards are consumed immediately in Telegram (user sees the summary
        # inline), so they are pre-marked as read. URL summaries default to unread
        # because they may be reviewed later in the mobile app.
        persistence = LLMSummaryPersistenceSettings(
            lang=chosen_lang,
            is_read=True,
        )

        async def _ensure_summary(summary: dict[str, Any]) -> dict[str, Any]:
            summary = mark_prompt_injection_metadata(summary, prompt)
            merge_summary_quality_metadata(
                summary,
                source_coverage="full",
                prompt_injection_suspected=summary.get("quality", {}).get(
                    "prompt_injection_suspected", False
                )
                if isinstance(summary.get("quality"), dict)
                else False,
            )
            return summary

        try:
            async with typing_indicator(self.response_formatter, message, action="typing"):
                return await self._workflow.execute_summary_workflow(
                    message=message,
                    req_id=req_id,
                    correlation_id=correlation_id,
                    interaction_config=interaction_config,
                    persistence=persistence,
                    repair_context=repair_context,
                    requests=requests,
                    notifications=notifications,
                    ensure_summary=_ensure_summary,
                )
        finally:
            if stream_coordinator is not None:
                await stream_coordinator.finalize()

    def _summary_streaming_enabled(self) -> bool:
        if not getattr(self.cfg.runtime, "summary_streaming_enabled", True):
            return False
        if getattr(self.cfg.runtime, "summary_streaming_mode", "section") != "section":
            return False
        telegram_cfg = getattr(self.cfg, "telegram", None)
        if telegram_cfg is None:
            return False
        if not getattr(telegram_cfg, "draft_streaming_enabled", True):
            return False

        scope = str(
            getattr(self.cfg.runtime, "summary_streaming_provider_scope", "openrouter")
        ).lower()
        if scope == "disabled":
            return False
        if scope == "all":
            return True
        provider_name = str(getattr(self.openrouter, "provider_name", "openrouter")).lower()
        return provider_name == scope
