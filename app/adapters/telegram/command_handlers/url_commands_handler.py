"""URL processing command handlers (/summarize, /summarize_all, /cancel).

This module handles commands related to URL summarization workflow,
including single URL processing, batch processing, and cancellation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from app.adapters.telegram.command_handlers.decorators import audit_command
from app.application.services.user_interaction_service import async_safe_update_user_interaction
from app.core.logging_utils import get_logger
from app.core.url_utils import extract_all_urls

if TYPE_CHECKING:
    from app.adapters.content.graph_url_processor import GraphURLProcessor as URLProcessor
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )
    from app.adapters.telegram.task_manager import UserTaskManager
    from app.adapters.telegram.url_handler import URLHandler
    from app.application.ports.requests import RequestRepositoryPort

logger = get_logger(__name__)


class URLProcessorProvider(Protocol):
    """Protocol for objects that supply URL processing collaborators."""

    url_processor: URLProcessor
    url_handler: URLHandler | None
    _task_manager: UserTaskManager | None


class URLCommandsHandler:
    """Implementation of URL processing commands.

    Handles /summarize, /summarize_all, and /cancel commands which control
    the URL summarization workflow.
    """

    def __init__(
        self,
        response_formatter: ResponseFormatter,
        processor_provider: URLProcessorProvider,
        *,
        request_repo: RequestRepositoryPort | None = None,
    ) -> None:
        """Initialize the URL commands handler.

        Args:
            response_formatter: Response formatter for sending messages.
            processor_provider: Object with url_processor, url_handler, and
                _task_manager attributes that can be dynamically accessed.
                This allows tests to modify these after initialization.
            request_repo: Optional repository for looking up requests by
                correlation_id (required for /retry command).
        """
        self._formatter = response_formatter
        self._processor_provider = processor_provider
        self._request_repo = request_repo

    @property
    def _url_processor(self) -> URLProcessor:
        return self._processor_provider.url_processor

    @property
    def _url_handler(self) -> URLHandler | None:
        return getattr(self._processor_provider, "url_handler", None)

    def _require_url_handler(self) -> URLHandler:
        handler = self._url_handler
        if handler is None:
            raise RuntimeError("url_handler is not wired into processor_provider")
        return handler

    @property
    def _task_manager(self) -> UserTaskManager | None:
        return getattr(self._processor_provider, "_task_manager", None)

    @audit_command("command_summarize", include_text=True)
    async def handle_summarize(
        self,
        ctx: CommandExecutionContext,
    ) -> tuple[str | None, bool]:
        """Handle /summarize command.

        Processes a URL from the message or prompts the user to send one.
        If multiple URLs are provided, asks for confirmation.

        Args:
            ctx: The command execution context.

        Returns:
            Tuple of (next_action, should_continue) indicating the state machine
            transition. next_action can be:
            - None: Processing complete
            - "awaiting_url": Waiting for user to send a URL
        """
        urls = extract_all_urls(ctx.text)

        if len(urls) > 1:
            # Multiple URLs - process directly in parallel (no confirmation prompt)
            url_handler = self._require_url_handler()
            valid_urls = await url_handler.apply_url_security_checks(
                ctx.message, urls, ctx.uid, ctx.correlation_id
            )
            if valid_urls:
                progress_id: int | None = None
                draft_enabled = False
                try:
                    enabled = self._formatter.is_draft_streaming_enabled()
                    draft_enabled = enabled if isinstance(enabled, bool) else False
                except Exception:
                    draft_enabled = False
                if not draft_enabled:
                    progress_id = await self._formatter.safe_reply_with_id(
                        ctx.message, f"Processing {len(valid_urls)} links in parallel..."
                    )
                await url_handler.process_url_batch(
                    ctx.message,
                    valid_urls,
                    ctx.uid,
                    ctx.correlation_id,
                    interaction_id=ctx.interaction_id,
                    start_time=ctx.start_time,
                    initial_message_id=progress_id,
                )
            logger.debug("multi_url_processed", extra={"uid": ctx.uid, "count": len(urls)})
            return None, False

        if len(urls) == 1:
            # Single URL - process directly
            await self._require_url_handler().handle_single_url(
                message=ctx.message,
                url=urls[0],
                correlation_id=ctx.correlation_id,
                interaction_id=ctx.interaction_id,
            )
            return None, False

        # No URL - prompt user
        await self._formatter.safe_reply(ctx.message, "Send a URL to summarize.")
        logger.debug("awaiting_url", extra={"uid": ctx.uid})

        if ctx.interaction_id:
            await async_safe_update_user_interaction(
                ctx.user_repo,
                interaction_id=ctx.interaction_id,
                response_sent=True,
                response_type="awaiting_url",
                start_time=ctx.start_time,
                logger_=logger,
            )
        return "awaiting_url", False

    @audit_command("command_summarize_all", include_text=True)
    async def handle_summarize_all(self, ctx: CommandExecutionContext) -> None:
        """Handle /summarize_all command.

        Processes multiple URLs from the message in sequence.

        Args:
            ctx: The command execution context.
        """
        urls = extract_all_urls(ctx.text)

        if len(urls) == 0:
            await self._formatter.safe_reply(
                ctx.message,
                "Send multiple URLs in one message after /summarize_all, "
                "separated by space or new line.",
            )
            if ctx.interaction_id:
                await async_safe_update_user_interaction(
                    ctx.user_repo,
                    interaction_id=ctx.interaction_id,
                    response_sent=True,
                    response_type="error",
                    error_occurred=True,
                    error_message="No URLs found",
                    start_time=ctx.start_time,
                    logger_=logger,
                )
            return

        # Use a single progress message that updates in-place
        progress_message_id: int | None = None
        draft_enabled = False
        try:
            enabled = self._formatter.is_draft_streaming_enabled()
            draft_enabled = enabled if isinstance(enabled, bool) else False
        except Exception:
            draft_enabled = False
        if not draft_enabled:
            progress_message_id = await self._formatter.safe_reply_with_id(
                ctx.message, f"🚀 Preparing to process {len(urls)} links..."
            )

        await self._require_url_handler().process_url_batch(
            ctx.message,
            urls,
            ctx.uid,
            ctx.correlation_id,
            interaction_id=ctx.interaction_id,
            start_time=ctx.start_time,
            initial_message_id=progress_message_id,
        )

    @audit_command("command_cancel")
    async def handle_cancel(self, ctx: CommandExecutionContext) -> None:
        """Handle /cancel command.

        Cancels any pending URL requests, multi-link confirmations,
        or active processing tasks.

        Args:
            ctx: The command execution context.
        """
        awaiting_cancelled = False
        active_cancelled = 0

        # Cancel pending requests via URL handler
        if self._url_handler is not None:
            awaiting_cancelled = await self._url_handler.cancel_pending_requests(ctx.uid)

        # Cancel active tasks via task manager
        if self._task_manager is not None:
            active_cancelled = await self._task_manager.cancel(ctx.uid, exclude_current=True)

        # Build response message
        cancelled_parts: list[str] = []
        if awaiting_cancelled:
            cancelled_parts.append("pending URL request")
        if active_cancelled:
            if active_cancelled == 1:
                cancelled_parts.append("ongoing request")
            else:
                cancelled_parts.append(f"{active_cancelled} ongoing requests")

        if cancelled_parts:
            if len(cancelled_parts) == 1:
                detail = cancelled_parts[0]
            else:
                detail = ", ".join(cancelled_parts[:-1]) + f", and {cancelled_parts[-1]}"
            reply_text = f"🛑 Cancelled your {detail}."
        else:
            reply_text = "ℹ️ No pending link requests to cancel."

        await self._formatter.safe_reply(ctx.message, reply_text)

        if ctx.interaction_id:
            response_type = (
                "cancelled" if (awaiting_cancelled or active_cancelled) else "cancel_none"
            )
            await async_safe_update_user_interaction(
                ctx.user_repo,
                interaction_id=ctx.interaction_id,
                response_sent=True,
                response_type=response_type,
                start_time=ctx.start_time,
                logger_=logger,
            )

    @audit_command("command_retry", include_text=True)
    async def handle_retry(self, ctx: CommandExecutionContext) -> tuple[str | None, bool]:
        """Handle /retry <error_id> — re-drive a previously failed URL request."""
        if self._request_repo is None:
            await self._formatter.safe_reply(ctx.message, "Retry is not available in this context.")
            return None, False

        parts = ctx.text.strip().split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await self._formatter.safe_reply(
                ctx.message,
                "Usage: /retry <error_id> — copy the Error ID shown in the failure message.",
            )
            return None, False

        correlation_id = parts[1].strip()
        request = await self._request_repo.async_get_latest_request_by_correlation_id(
            correlation_id
        )
        if request is None or request.get("user_id") != ctx.uid:
            await self._formatter.safe_reply(
                ctx.message, f"No failed request found for Error ID: {correlation_id}"
            )
            return None, False

        status = request.get("status", "")
        if status != "error":
            await self._formatter.safe_reply(
                ctx.message,
                f"Request {correlation_id} has status '{status}', not 'error'. Nothing to retry.",
            )
            return None, False

        input_url = request.get("input_url") or ""
        if not input_url:
            await self._formatter.safe_reply(
                ctx.message, f"No URL found for request {correlation_id}."
            )
            return None, False

        url_handler = self._require_url_handler()
        retry_cid = f"{correlation_id}-retry-1"
        logger.info(
            "command_retry_started",
            extra={"cid": retry_cid, "original_cid": correlation_id, "uid": ctx.uid},
        )
        await url_handler.handle_single_url(
            message=ctx.message,
            url=input_url,
            correlation_id=retry_cid,
            interaction_id=ctx.interaction_id,
            batch_mode=False,
        )
        return None, False
