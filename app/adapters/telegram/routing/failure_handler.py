"""Failure handling for Telegram routing."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.adapters.content.llm_response_workflow import ConcurrencyTimeoutError

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )

    from .interactions import MessageInteractionRecorder

logger = logging.getLogger("app.adapters.telegram.message_router")


class MessageRouteFailureHandler:
    """Handle cancellations, audits, and user-facing error mapping."""

    def __init__(
        self,
        response_formatter: ResponseFormatter,
        audit_func: Callable[[str, str, dict], None],
        interaction_recorder: MessageInteractionRecorder,
    ) -> None:
        self.response_formatter = response_formatter
        self._audit = audit_func
        self.interaction_recorder = interaction_recorder

    async def handle_cancelled(
        self,
        *,
        correlation_id: str,
        uid: int,
        interaction_id: int,
        start_time: float,
    ) -> None:
        logger.info("message_processing_cancelled", extra={"cid": correlation_id, "uid": uid})
        await self.interaction_recorder.update(
            interaction_id,
            response_sent=False,
            response_type="cancelled",
            start_time=start_time,
        )

    async def handle_exception(
        self,
        *,
        message: Any,
        error: Exception,
        correlation_id: str,
        interaction_id: int,
        start_time: float,
    ) -> None:
        logger.exception("handler_error", extra={"cid": correlation_id})
        self._audit_route_exception(correlation_id, error)
        await self._notify_route_exception(message, correlation_id, error)
        await self.interaction_recorder.update(
            interaction_id,
            response_sent=True,
            response_type="error",
            error_occurred=True,
            error_message=str(error)[:500],
            start_time=start_time,
        )

    def _audit_route_exception(self, correlation_id: str, error: Exception) -> None:
        try:
            self._audit("ERROR", "unhandled_error", {"cid": correlation_id, "error": str(error)})
        except Exception as audit_error:
            logger.error(
                "audit_logging_failed",
                extra={
                    "cid": correlation_id,
                    "original_error": str(error),
                    "audit_error": str(audit_error),
                    "audit_error_type": type(audit_error).__name__,
                },
            )

    async def _notify_route_exception(
        self,
        message: Any,
        correlation_id: str,
        error: Exception,
    ) -> None:
        error_text = str(error)
        error_lower = error_text.lower()

        if isinstance(error, ConcurrencyTimeoutError):
            await self.response_formatter.send_error_notification(
                message,
                "rate_limit",
                correlation_id,
                details=(
                    "The system is currently handling too many requests. "
                    "Please try again in a few moments."
                ),
            )
            return

        if "timeout" in error_lower or isinstance(error, TimeoutError):
            await self.response_formatter.send_error_notification(
                message,
                "timeout",
                correlation_id,
                details=(
                    "The request timed out. The article might be too large or "
                    "the service is temporarily slow."
                ),
            )
            return

        if "rate limit" in error_lower or "429" in error_text:
            await self.response_formatter.send_error_notification(
                message,
                "rate_limit",
                correlation_id,
                details="The service is currently busy. Please retry in a few minutes.",
            )
            return

        if any(keyword in error_lower for keyword in ("connection", "network", "unreachable")):
            await self.response_formatter.send_error_notification(
                message,
                "network_error",
                correlation_id,
                details=(
                    "A network error occurred. Please check your connection or try again later."
                ),
            )
            return

        if any(keyword in error_lower for keyword in ("database", "postgres", "disk")):
            await self.response_formatter.send_error_notification(
                message,
                "database_error",
                correlation_id,
                details="An internal database error occurred. This is usually temporary.",
            )
            return

        await self.response_formatter.send_error_notification(
            message,
            "unexpected_error",
            correlation_id,
        )
