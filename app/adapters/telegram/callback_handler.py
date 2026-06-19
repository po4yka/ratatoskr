"""Callback handler for inline button interactions."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.callback_action_registry import CallbackActionRegistry
from app.adapters.telegram.callback_actions import CallbackActionService
from app.adapters.telegram.summary_followup import SummaryFollowupManager
from app.core.logging_utils import generate_correlation_id, get_logger

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.telegram.url_handler import URLHandler
    from app.db.session import Database
    from app.infrastructure.search.hybrid_search_service import HybridSearchService

logger = get_logger(__name__)


class CallbackHandler:
    """Routes callback actions and delegates execution to focused services."""

    def __init__(
        self,
        db: Database,
        response_formatter: ResponseFormatter,
        url_handler: URLHandler | None = None,
        hybrid_search: HybridSearchService | None = None,
        lang: str = "en",
        request_repo: Any | None = None,
        summary_repo: Any | None = None,
        crawl_result_repo: Any | None = None,
    ) -> None:
        self.db = db
        self.response_formatter = response_formatter
        self.url_handler = url_handler
        self.hybrid_search = hybrid_search
        self._lang = lang

        self._recent_clicks: dict[tuple[int, str], float] = {}
        self._click_cooldown_seconds = 1.0

        self._actions = CallbackActionService(
            db=db,
            response_formatter=response_formatter,
            url_handler=url_handler,
            hybrid_search=hybrid_search,
            lang=lang,
            request_repo=request_repo,
            summary_repo=summary_repo,
        )
        self._registry = CallbackActionRegistry()
        self._register_default_actions()

        self._followup = SummaryFollowupManager(
            crawl_result_repo=crawl_result_repo,
            response_formatter=response_formatter,
            url_handler=url_handler,
            lang=lang,
            load_summary_payload=self._actions.load_summary_payload,
        )

    def _register_default_actions(self) -> None:
        self._registry.register("dg", self._actions.handle_digest_full_summary)
        self._registry.register("export", self._actions.handle_export)
        self._registry.register("translate", self._actions.handle_translate)
        self._registry.register("similar", self._actions.handle_find_similar)
        self._registry.register("save", self._actions.handle_toggle_save)
        self._registry.register("rate", self._actions.handle_rate)
        self._registry.register("more", self._actions.handle_more)
        self._registry.register("ask", self._handle_followup_entry)
        self._registry.register("rel", self._actions.handle_show_related_summary)
        self._registry.register("retry", self._actions.handle_retry)

    async def handle_callback(
        self,
        callback_query: Any,
        uid: int,
        callback_data: str,
    ) -> bool:
        """Route callback to appropriate handler.

        Returns:
            True if callback was handled, False otherwise.
        """
        click_key = (uid, callback_data)
        now = time.time()
        last_click = self._recent_clicks.get(click_key, 0.0)
        if now - last_click < self._click_cooldown_seconds:
            logger.debug(
                "callback_debounced",
                extra={"uid": uid, "data": callback_data, "cooldown": self._click_cooldown_seconds},
            )
            return True

        self._recent_clicks[click_key] = now
        if len(self._recent_clicks) > 1000:
            cutoff = now - 60.0
            self._recent_clicks = {k: v for k, v in self._recent_clicks.items() if v > cutoff}

        message = getattr(callback_query, "message", None)
        if not message:
            return False

        correlation_id = generate_correlation_id()

        parts = callback_data.split(":")
        action = parts[0] if parts else ""
        logger.info(
            "callback_action_received",
            extra={"uid": uid, "action": action, "data": callback_data, "cid": correlation_id},
        )

        handler = self._registry.resolve(action)
        if handler is None:
            logger.warning(
                "unknown_callback_action",
                extra={"action": action, "uid": uid, "cid": correlation_id},
            )
            return False

        try:
            return await handler(message, uid, parts, correlation_id)
        except Exception as exc:
            logger.exception(
                "callback_handler_error",
                extra={"action": action, "uid": uid, "error": str(exc), "cid": correlation_id},
            )
            await self.response_formatter.send_error_notification(
                message,
                "unexpected_error",
                correlation_id,
                details="The button action could not be completed.",
            )
            return True

    async def has_pending_followup(self, uid: int) -> bool:
        """Return True when user has an active follow-up Q&A session."""
        return await self._followup.has_pending(uid)

    async def clear_pending_followup(self, uid: int) -> None:
        """Clear active follow-up session for a user."""
        await self._followup.clear(uid)

    async def handle_followup_question(
        self,
        message: Any,
        uid: int,
        question: str,
        correlation_id: str,
    ) -> bool:
        """Answer a free-form follow-up question against stored summary + source context."""
        return await self._followup.answer(
            message=message,
            uid=uid,
            question=question,
            correlation_id=correlation_id,
        )

    async def _handle_followup_entry(
        self,
        message: Any,
        uid: int,
        parts: list[str],
        correlation_id: str,
    ) -> bool:
        """Start follow-up Q&A mode for a summary."""
        if len(parts) < 2:
            return False

        summary_id = ":".join(parts[1:]).strip()
        if not summary_id:
            return False

        await self._followup.start_session(
            message=message,
            uid=uid,
            summary_id=summary_id,
            correlation_id=correlation_id,
        )
        return True

    async def _activate_followup_session(self, uid: int, summary_id: str) -> None:
        await self._followup.activate(uid, summary_id)

    async def _load_summary_payload(
        self,
        summary_id: str,
        *,
        correlation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Compatibility wrapper for tests that patch this callback."""
        return await self._actions.load_summary_payload(
            summary_id,
            correlation_id=correlation_id,
        )
