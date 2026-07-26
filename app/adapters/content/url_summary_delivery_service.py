"""Summary delivery, persistence, and tracked-task helpers for URL flows."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from app.adapters.content.url_flow_models import (
    URLFlowContext,
    URLProcessingFlowResult,
    create_chunk_llm_stub,
)
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.db.user_interactions import async_safe_update_user_interaction

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )


class URLSummaryDeliveryService:
    """Own final summary delivery and persistence for URL-processing flows."""

    def __init__(
        self,
        *,
        cfg: Any,
        db: Any,
        response_formatter: ResponseFormatter,
        summary_repo: Any,
        audit_func: Any,
        request_repo: Any = None,
    ) -> None:
        self._cfg = cfg
        self._db = db
        self._response_formatter = response_formatter
        self._summary_repo = summary_repo
        self._audit = audit_func
        self._request_repo = request_repo
        self._tracked_tasks: set[asyncio.Task[Any]] = set()

    async def aclose(self, timeout: float = 5.0) -> None:
        """Drain tracked delivery tasks before shutdown."""
        await self.drain_tasks(
            self._tracked_tasks,
            timeout=timeout,
            timeout_event="url_summary_delivery_shutdown_timeout",
            complete_event="url_summary_delivery_shutdown_complete",
        )

    async def deliver_summary(
        self,
        *,
        message: Any,
        summary_result: Any,
        context: URLFlowContext,
        correlation_id: str | None,
        interaction_id: int | None,
        silent: bool,
        batch_mode: bool,
    ) -> URLProcessingFlowResult:
        """Deliver a completed summary and persist chunked results if required."""
        summary_json = summary_result.summary if summary_result else None
        if summary_json is None:
            return URLProcessingFlowResult(success=False)

        persist_task = self.schedule_chunk_persistence_if_needed(
            context=context,
            summary_json=summary_json,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            silent=silent,
        )

        if not silent and not batch_mode:
            llm_result = (
                summary_result.llm_result
                if getattr(summary_result, "llm_result", None) is not None
                else None
            ) or create_chunk_llm_stub(self._cfg)
            bot_reply_msg_id = await self._response_formatter.send_structured_summary_response(
                message,
                summary_json,
                llm_result,
                chunks=len(context.chunks) if context.should_chunk and context.chunks else None,
                summary_id=f"req:{context.req_id}" if context.req_id else None,
                correlation_id=correlation_id,
            )
            if bot_reply_msg_id and context.req_id:
                await self._persist_bot_reply_message_id(
                    req_id=context.req_id,
                    bot_reply_msg_id=bot_reply_msg_id,
                    correlation_id=correlation_id,
                )

        if (silent or batch_mode) and persist_task is not None:
            await self.await_task(persist_task)

        return URLProcessingFlowResult.from_summary(
            summary_json,
            request_id=context.req_id,
        )

    async def send_processing_failure(
        self,
        *,
        message: Any,
        url_text: str,
        correlation_id: str | None,
        silent: bool,
        batch_mode: bool,
        error_type: str = "processing_failed",
    ) -> URLProcessingFlowResult:
        """Notify the user of a terminal failure.

        ``error_type`` selects the message template (see
        ``send_error_notification``). It defaults to ``processing_failed`` (the
        LLM parse/repair copy); extraction/content-fetch failures pass
        ``empty_content`` so the user is told the page couldn't be retrieved
        rather than that the AI failed to parse it -- the LLM was never reached.
        """
        logger.error(
            "summarization_failed",
            extra={
                "cid": correlation_id,
                "url": redact_url_for_logging(url_text),
                "error_type": error_type,
            },
        )
        if not silent and not batch_mode:
            await self._response_formatter.send_error_notification(
                message,
                error_type,
                correlation_id or "unknown",
            )
        return URLProcessingFlowResult(success=False)

    def schedule_chunk_persistence_if_needed(
        self,
        *,
        context: URLFlowContext,
        summary_json: dict[str, Any],
        correlation_id: str | None,
        interaction_id: int | None,
        silent: bool,
    ) -> asyncio.Task[Any] | None:
        if not (context.should_chunk and context.chunks):
            return None
        return self.schedule_task(
            self._tracked_tasks,
            self.persist_summary(
                req_id=context.req_id,
                chosen_lang=context.chosen_lang,
                summary_json=summary_json,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
                silent=silent,
            ),
            correlation_id,
            "persist_summary",
            schedule_error_event="persistence_task_schedule_failed",
            task_error_event="persistence_task_failed",
        )

    async def persist_summary(
        self,
        *,
        req_id: int,
        chosen_lang: str,
        summary_json: dict[str, Any],
        correlation_id: str | None,
        interaction_id: int | None,
        silent: bool,
    ) -> None:
        try:
            finalize_result = await self._summary_repo.async_finalize_request_summary(
                request_id=req_id,
                lang=chosen_lang,
                json_payload=summary_json,
                is_read=not silent,
            )
            new_version = finalize_result.version
            self._audit("INFO", "summary_upserted", {"request_id": req_id, "version": new_version})

            if interaction_id:
                await async_safe_update_user_interaction(
                    self._db,
                    interaction_id=interaction_id,
                    response_sent=True,
                    response_type="summary",
                    request_id=req_id,
                )
        except Exception as exc:
            logger.error(
                "summary_persistence_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            raise

    def schedule_task(
        self,
        task_registry: set[asyncio.Task[Any]],
        coro: Coroutine[Any, Any, Any],
        correlation_id: str | None,
        label: str,
        *,
        schedule_error_event: str,
        task_error_event: str,
    ) -> asyncio.Task[Any] | None:
        try:
            task: asyncio.Task[Any] = asyncio.create_task(coro)
            task_registry.add(task)
            task.add_done_callback(task_registry.discard)
        except RuntimeError as exc:
            logger.error(
                schedule_error_event,
                extra={"cid": correlation_id, "label": label, "error": str(exc)},
            )
            return None

        def _log_task_error(done_task: asyncio.Task[Any]) -> None:
            if done_task.cancelled():
                return
            exc = done_task.exception()
            if exc:
                logger.error(
                    task_error_event,
                    extra={"cid": correlation_id, "label": label, "error": str(exc)},
                )

        task.add_done_callback(_log_task_error)
        return task

    async def await_task(self, task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        try:
            await task
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.error("persistence_task_failed", extra={"error": str(exc)})

    async def drain_tasks(
        self,
        task_registry: set[asyncio.Task[Any]],
        *,
        timeout: float,
        timeout_event: str,
        complete_event: str,
    ) -> None:
        if not task_registry:
            return

        logger.info(
            complete_event.replace("_complete", "_draining"),
            extra={"task_count": len(task_registry)},
        )
        tasks = list(task_registry)
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            logger.warning(timeout_event, extra={"pending": len(task_registry)})
        logger.info(complete_event)

    async def _persist_bot_reply_message_id(
        self,
        *,
        req_id: int,
        bot_reply_msg_id: int,
        correlation_id: str | None,
    ) -> None:
        if self._request_repo is None:
            return
        try:
            await self._request_repo.async_update_bot_reply_message_id(req_id, bot_reply_msg_id)
        except Exception as exc:
            logger.warning(
                "bot_reply_msg_id_persist_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
