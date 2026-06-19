"""Interaction persistence helpers for Telegram routing."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.application.services.user_interaction_service import async_safe_update_user_interaction

if TYPE_CHECKING:
    from .models import PreparedRouteContext

logger = logging.getLogger("app.adapters.telegram.message_router")


class MessageInteractionRecorder:
    """Persist and update interaction rows for Telegram routing."""

    def __init__(self, user_repo: Any, structured_output_enabled: bool) -> None:
        self.user_repo = user_repo
        self._structured_output_enabled = structured_output_enabled

    async def log(self, context: PreparedRouteContext) -> int:
        """Persist initial interaction state and return its identifier.

        Returns the new interaction row ID on success, or 0 if the insert
        fails.  Callers should treat a 0 return as "interaction not recorded"
        and skip any downstream interaction updates (the guard in update()
        handles this automatically).  The failure is logged as a WARNING so
        it remains visible without interrupting the main request flow.
        """
        try:
            return await self.user_repo.async_insert_user_interaction(
                user_id=context.uid,
                chat_id=context.chat_id,
                message_id=context.message_id,
                interaction_type=context.interaction_type,
                command=context.command,
                input_text=context.text[:1000] if context.text else None,
                input_url=context.first_url,
                has_forward=context.has_forward,
                forward_from_chat_id=context.forward_from_chat_id,
                forward_from_chat_title=context.forward_from_chat_title,
                forward_from_message_id=context.forward_from_message_id,
                media_type=context.media_type,
                correlation_id=context.correlation_id,
                structured_output_enabled=self._structured_output_enabled,
            )
        except Exception as exc:
            logger.warning(
                "user_interaction_log_failed",
                extra={
                    "error": str(exc),
                    "user_id": context.uid,
                    "interaction_type": context.interaction_type,
                    "cid": context.correlation_id,
                },
            )
            return 0

    async def update(
        self,
        interaction_id: int,
        *,
        response_sent: bool | None = None,
        response_type: str | None = None,
        error_occurred: bool | None = None,
        error_message: str | None = None,
        request_id: int | None = None,
        start_time: float,
    ) -> None:
        """Update a previously persisted interaction."""
        if not interaction_id:
            return

        await async_safe_update_user_interaction(
            self.user_repo,
            interaction_id=interaction_id,
            response_sent=response_sent,
            response_type=response_type,
            error_occurred=error_occurred,
            error_message=error_message,
            request_id=request_id,
            start_time=start_time,
            logger_=logger,
        )
