"""Interactive summary service for Telegram and similar flows."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.adapters.content.llm_response_workflow import (
    AttemptContext,
    LLMInteractionConfig,
    LLMSummaryPersistenceSettings,
    LLMWorkflowNotifications,
)
from app.adapters.external.formatting.single_url_progress_formatter import (
    SingleURLProgressFormatter,
)
from app.core.logging_utils import get_logger
from app.core.summary_contract_impl.quality_metadata import merge_summary_quality_metadata
from app.db.user_interactions import async_safe_update_user_interaction
from app.domain.models.request import RequestStatus
from app.utils.progress_message_updater import ProgressMessageUpdater
from app.utils.typing_indicator import typing_indicator

from .summarization_models import InteractiveSummaryRequest, InteractiveSummaryResult
from .summary_request_factory import mark_prompt_injection_metadata

if TYPE_CHECKING:
    from .pure_summary_service import PureSummaryService
    from .summarization_runtime import SummarizationRuntime
    from .summary_request_factory import SummaryExecutionPlan, SummaryRequestFactory

logger = get_logger(__name__)


@dataclass(slots=True)
class _InteractiveSummaryState:
    llm_result: Any | None = None


class _InteractiveSummaryCallbacks:
    """Named workflow callbacks for interactive summary execution."""

    def __init__(
        self,
        *,
        runtime: SummarizationRuntime,
        request: InteractiveSummaryRequest,
        state: _InteractiveSummaryState,
    ) -> None:
        self._runtime = runtime
        self._request = request
        self._state = state

    @property
    def llm_result(self) -> Any | None:
        return self._state.llm_result

    async def on_attempt(self, llm_result: Any) -> None:
        self._state.llm_result = llm_result

    async def on_success(self, summary: dict[str, Any], llm_result: Any) -> None:
        _ = llm_result
        self._runtime.insights_generator.update_last_summary(summary)

    async def ensure_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        shaped = await self._runtime.metadata_helper.ensure_summary_metadata(
            summary,
            self._request.req_id,
            self._request.content_text,
            self._request.correlation_id,
            self._request.chosen_lang,
        )
        shaped = mark_prompt_injection_metadata(shaped, self._request.content_text)
        merge_summary_quality_metadata(
            shaped,
            source_coverage=self._request.source_coverage,
            extraction_quality=self._request.extraction_quality,
            extraction_confidence=self._request.extraction_confidence,
            prompt_injection_suspected=shaped.get("quality", {}).get(
                "prompt_injection_suspected", False
            )
            if isinstance(shaped.get("quality"), dict)
            else False,
        )
        return shaped

    async def on_completion(self, llm_result: Any, attempt: Any) -> None:
        await self._runtime.response_formatter.send_llm_completion_notification(
            self._request.message,
            llm_result,
            self._request.correlation_id,
            silent=attempt.silent,
        )

    @property
    def _silent(self) -> bool:
        return self._request.silent

    async def _notify_error(self, event: str, details: str | None) -> None:
        """Send error notification; no-op in silent mode."""
        if self._silent:
            return
        await self._runtime.response_formatter.send_error_notification(
            self._request.message,
            event,
            self._request.correlation_id or "unknown",
            details=details,
        )

    async def on_llm_error(self, llm_result: Any, details: str | None) -> None:
        await self._notify_error("llm_error", details)

    async def on_repair_failure(self) -> None:
        await self._notify_error(
            "processing_failed", "Unable to repair invalid JSON returned by the model"
        )

    async def on_parsing_failure(self) -> None:
        await self._notify_error(
            "processing_failed", "Model did not produce valid summary output after retries"
        )

    async def on_retry(self) -> None:
        if self._request.on_phase_change:
            await self._request.on_phase_change("retrying", None, None, None)

    def build_llm_error_kwargs(self, llm_result: Any, details: str | None) -> dict[str, Any]:
        return {
            "response_sent": True,
            "response_type": "error",
            "error_occurred": True,
            "error_message": details or f"LLM error: {llm_result.error_text or 'Unknown error'}",
            "request_id": self._request.req_id,
        }

    def build_notifications(self) -> LLMWorkflowNotifications:
        return LLMWorkflowNotifications(
            completion=self.on_completion,
            llm_error=self.on_llm_error,
            repair_failure=self.on_repair_failure,
            parsing_failure=self.on_parsing_failure,
            retry=self.on_retry,
        )

    def build_interaction_config(self) -> LLMInteractionConfig:
        return LLMInteractionConfig(
            interaction_id=self._request.interaction_id,
            success_kwargs={
                "response_sent": True,
                "response_type": "summary",
                "request_id": self._request.req_id,
            },
            llm_error_builder=self.build_llm_error_kwargs,
            repair_failure_kwargs={
                "response_sent": True,
                "response_type": "error",
                "error_occurred": True,
                "error_message": "Invalid summary format",
                "request_id": self._request.req_id,
            },
            parsing_failure_kwargs={
                "response_sent": True,
                "response_type": "error",
                "error_occurred": True,
                "error_message": "Invalid summary format",
                "request_id": self._request.req_id,
            },
        )

    def build_persistence_settings(self) -> LLMSummaryPersistenceSettings:
        return LLMSummaryPersistenceSettings(
            lang=self._request.chosen_lang,
            is_read=True,
            insights_getter=self.get_insights_from_summary,
            defer_write=self._request.defer_persistence,
        )

    def get_insights_from_summary(self, summary: dict[str, Any]) -> dict[str, Any] | None:
        return self._runtime.insights_generator.insights_from_summary(summary)


class InteractiveSummaryService:
    """Interactive summary orchestration for Telegram-style flows."""

    def __init__(
        self,
        *,
        runtime: SummarizationRuntime,
        request_factory: SummaryRequestFactory,
        pure_summary_service: PureSummaryService,
    ) -> None:
        self._runtime = runtime
        self._request_factory = request_factory
        self._pure_summary_service = pure_summary_service

    async def summarize(self, request: InteractiveSummaryRequest) -> InteractiveSummaryResult:
        """Run the interactive summary workflow and return summary plus LLM metadata."""
        if not request.content_text or not request.content_text.strip():
            await self._handle_empty_content_error(request)
            return InteractiveSummaryResult(
                summary=None,
                llm_result=None,
                served_from_cache=False,
                model_used=None,
            )

        state = _InteractiveSummaryState()
        callbacks = _InteractiveSummaryCallbacks(
            runtime=self._runtime,
            request=request,
            state=state,
        )
        self._runtime.insights_generator.reset_state()
        plan = await self._request_factory.prepare_interactive_request(
            request,
            callbacks=callbacks,
        )

        cached_summary = await self._runtime.cache_helper.get_cached_summary(
            request.url_hash,
            request.chosen_lang,
            plan.base_model,
            request.correlation_id,
        )
        if cached_summary is not None:
            shaped, llm_stub = await self._finalize_cached_summary(
                request=request,
                cached_summary=cached_summary,
                plan=plan,
                callbacks=callbacks,
            )
            return InteractiveSummaryResult(
                summary=shaped,
                llm_result=llm_stub,
                served_from_cache=True,
                model_used=plan.base_model,
            )

        await self._runtime.response_formatter.send_llm_start_notification(
            request.message,
            plan.base_model,
            len(request.content_text),
            self._runtime.cfg.openrouter.structured_output_mode,
            url=request.url,
            silent=request.silent,
        )

        try:
            summary = await self._execute_summary_with_progress(
                request=request,
                plan=plan,
                callbacks=callbacks,
            )
        finally:
            if plan.stream_coordinator is not None:
                await plan.stream_coordinator.finalize()

        llm_result = callbacks.llm_result
        model_used = getattr(llm_result, "model", plan.base_model) if llm_result else None
        if summary and request.url_hash and model_used:
            await self._runtime.cache_helper.write_summary_cache(
                request.url_hash,
                model_used,
                request.chosen_lang,
                summary,
            )
        return InteractiveSummaryResult(
            summary=summary,
            llm_result=llm_result,
            served_from_cache=False,
            model_used=model_used,
        )

    async def _execute_summary_with_progress(
        self,
        *,
        request: InteractiveSummaryRequest,
        plan: SummaryExecutionPlan,
        callbacks: _InteractiveSummaryCallbacks,
    ) -> dict[str, Any] | None:
        """Run the workflow under either progress updates or a typing indicator."""
        use_progress = request.progress_tracker is not None
        updater: ProgressMessageUpdater | None = None
        typing_ctx: Any = None
        start_time = time.time()

        try:
            if use_progress and request.progress_tracker is not None:
                updater = ProgressMessageUpdater(request.progress_tracker, request.message)
                await updater.start(
                    self._build_progress_formatter(
                        content_length=len(plan.content_for_summary),
                        model=plan.base_model,
                        phase="analyzing",
                        content_tier=plan.content_tier,
                        content_lang=request.chosen_lang,
                    )
                )
            else:
                typing_ctx = typing_indicator(
                    self._runtime.response_formatter,
                    request.message,
                    action="typing",
                )
                await typing_ctx.__aenter__()

            summary = await self._runtime.workflow.execute_summary_workflow(
                message=request.message,
                req_id=request.req_id,
                correlation_id=request.correlation_id,
                interaction_config=plan.interaction_config,
                persistence=plan.persistence,
                repair_context=plan.repair_context,
                requests=plan.requests,
                notifications=plan.notifications,
                ensure_summary=callbacks.ensure_summary,
                on_attempt=callbacks.on_attempt,
                on_success=callbacks.on_success,
                defer_persistence=request.defer_persistence,
            )

            if summary and self._runtime.cfg.runtime.summary_two_pass_enabled:
                if use_progress and updater is not None:
                    await updater.update_formatter(
                        self._build_progress_formatter(
                            content_length=len(plan.content_for_summary),
                            model=plan.base_model,
                            phase="enriching",
                            content_tier=plan.content_tier,
                            content_lang=request.chosen_lang,
                        )
                    )
                summary = await self._pure_summary_service.enrich_two_pass(
                    summary,
                    content_text=plan.content_for_summary,
                    chosen_lang=request.chosen_lang,
                    correlation_id=request.correlation_id,
                )

            elapsed_total = time.time() - start_time
            if use_progress and updater is not None:
                success_msg = SingleURLProgressFormatter.format_llm_complete(
                    model=plan.base_model,
                    elapsed_sec=elapsed_total,
                    success=summary is not None,
                    correlation_id=request.correlation_id if summary is None else None,
                )
                await updater.finalize(success_msg)
            elif typing_ctx:
                await typing_ctx.__aexit__(None, None, None)
            return summary
        except Exception:
            if use_progress and updater is not None:
                error_msg = SingleURLProgressFormatter.format_llm_complete(
                    model=plan.base_model,
                    elapsed_sec=time.time() - start_time,
                    success=False,
                    error_msg="Processing failed",
                    correlation_id=request.correlation_id,
                )
                await updater.finalize(error_msg)
            elif typing_ctx:
                await typing_ctx.__aexit__(None, None, None)
            raise

    async def _finalize_cached_summary(
        self,
        *,
        request: InteractiveSummaryRequest,
        cached_summary: dict[str, Any],
        plan: SummaryExecutionPlan,
        callbacks: _InteractiveSummaryCallbacks,
    ) -> tuple[dict[str, Any], Any]:
        """Finalize response flow when Redis contains a cached summary."""
        llm_stub = self._runtime.cache_helper.build_cache_stub(plan.base_model)
        ctx = AttemptContext(
            message=request.message,
            llm=llm_stub,
            req_id=request.req_id,
            correlation_id=request.correlation_id,
            interaction_config=plan.interaction_config,
            persistence=plan.persistence,
            ensure_summary=callbacks.ensure_summary,
            on_success=callbacks.on_success,
            defer_persistence=request.defer_persistence,
        )
        shaped = await self._runtime.workflow.finalize_success(ctx, cached_summary)
        if not request.silent:
            await self._runtime.response_formatter.send_cached_summary_notification(
                request.message,
                silent=request.silent,
            )
        if request.url_hash:
            await self._runtime.cache_helper.write_summary_cache(
                request.url_hash,
                plan.base_model,
                request.chosen_lang,
                shaped,
            )
        return shaped, llm_stub

    async def _handle_empty_content_error(self, request: InteractiveSummaryRequest) -> None:
        """Persist and notify the empty-content failure path."""
        logger.error(
            "empty_content_for_llm",
            extra={"cid": request.correlation_id, "content_source": "unknown"},
        )
        await self._runtime.request_repo.async_update_request_status(
            request.req_id, RequestStatus.ERROR
        )
        await self._runtime.response_formatter.send_error_notification(
            request.message,
            "empty_content",
            request.correlation_id,
        )

        if request.interaction_id:
            await async_safe_update_user_interaction(
                self._runtime.db,
                interaction_id=request.interaction_id,
                response_sent=True,
                response_type="error",
                error_occurred=True,
                error_message="No meaningful content extracted from URL",
                request_id=request.req_id,
                logger_=logger,
            )

    @staticmethod
    def _build_progress_formatter(
        *,
        content_length: int,
        model: str,
        phase: str,
        content_tier: str | None = None,
        content_lang: str | None = None,
    ) -> Any:
        def _formatter(elapsed: float) -> str:
            return SingleURLProgressFormatter.format_llm_progress(
                content_length=content_length,
                model=model,
                elapsed_sec=elapsed,
                phase=phase,
                content_tier=content_tier,
                content_lang=content_lang,
            )

        return _formatter
