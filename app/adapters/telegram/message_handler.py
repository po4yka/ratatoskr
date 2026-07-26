"""Refactored message handler using modular components."""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.task_manager import UserTaskManager
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import generate_correlation_id, get_logger

if TYPE_CHECKING:
    from app.adapters.telegram.access_controller import AccessController
    from app.adapters.telegram.callback_handler import CallbackHandler
    from app.adapters.telegram.command_dispatcher import TelegramCommandDispatcher
    from app.adapters.telegram.message_router import MessageRouter
    from app.adapters.telegram.url_handler import URLHandler
    from app.application.ports.audit import AuditLogRepositoryPort
    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)


class _NullAuditLogRepository:
    async def async_insert_audit_log(self, **_kwargs: object) -> None:
        return None


class MessageHandler:
    """Refactored message handler using modular components."""

    def __init__(
        self,
        cfg: AppConfig,
        db: Database | None,
        *,
        audit_repo: AuditLogRepositoryPort | None,
        task_manager: UserTaskManager | None,
        access_controller: AccessController,
        url_handler: URLHandler,
        command_dispatcher: TelegramCommandDispatcher,
        callback_handler: CallbackHandler,
        message_router: MessageRouter,
        url_processor: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.audit_repo = audit_repo or _NullAuditLogRepository()
        self.task_manager = task_manager or UserTaskManager()
        self.access_controller = access_controller
        self.url_handler = url_handler
        self.url_processor = url_processor or getattr(url_handler, "url_processor", None)
        self.command_processor = command_dispatcher
        self.callback_handler = callback_handler
        self.message_router = message_router

    async def handle_message(self, message: Any) -> None:
        """Main message handling entry point."""
        await self.message_router.route_message(message)

    async def handle_callback_query(self, callback_query: Any) -> None:
        """Handle inline button callback queries."""
        try:
            # Extract callback data and user info
            data = getattr(callback_query, "data", None)
            from_user = getattr(callback_query, "from_user", None)
            message = getattr(callback_query, "message", None)

            if not data or not from_user or not message:
                logger.warning("invalid_callback_query", extra={"has_data": data is not None})
                return

            uid = from_user.id
            callback_data = data.decode() if isinstance(data, bytes) else str(data)

            # Access control: inline button taps must pass the same
            # ALLOWED_USER_IDS gate as text/command messages. Callback queries do
            # not go through MessageRouter.route_message, so without this check any
            # Telegram user who can tap a button on one of the bot's messages could
            # invoke privileged callback actions (export, retry, translate, ...)
            # outside the allowlist, rate limiter, and audit hook.
            correlation_id = generate_correlation_id()
            if not await self.access_controller.check_access(
                uid, message, correlation_id, 0, time.monotonic()
            ):
                with contextlib.suppress(Exception):
                    await callback_query.answer("Access denied.", show_alert=True)
                logger.warning("callback_access_denied", extra={"uid": uid, "cid": correlation_id})
                return

            logger.info(
                "callback_query_received",
                extra={"uid": uid, "data": callback_data},
            )

            # Answer the callback query to remove the loading state
            try:
                await callback_query.answer()
            except Exception as e:
                raise_if_cancelled(e)
                logger.warning("callback_answer_failed", extra={"error": str(e)})

            # Route to the unified callback handler for all other actions
            handled = await self.callback_handler.handle_callback(
                callback_query, uid, callback_data
            )
            if not handled:
                logger.warning("unhandled_callback_data", extra={"data": callback_data})

        except Exception as e:
            raise_if_cancelled(e)
            logger.exception("callback_query_handler_failed", extra={"error": str(e)})

    def _audit(self, level: str, event: str, details: dict) -> None:
        """Audit log helper (background async)."""
        import asyncio

        async def _do_audit() -> None:
            try:
                await self.audit_repo.async_insert_audit_log(
                    log_level=level, event_type=event, details=details
                )
            except Exception as e:
                raise_if_cancelled(e)
                logger.warning("audit_persist_failed", extra={"error": str(e), "event": event})

        try:
            loop = asyncio.get_running_loop()
            task = loop.create_task(_do_audit())
            if not hasattr(self, "_audit_tasks"):
                self._audit_tasks: set[asyncio.Task] = set()
            self._audit_tasks.add(task)
            task.add_done_callback(self._audit_tasks.discard)
        except RuntimeError as exc:
            logger.debug("audit_task_schedule_skipped", extra={"event": event, "error": str(exc)})
            return
