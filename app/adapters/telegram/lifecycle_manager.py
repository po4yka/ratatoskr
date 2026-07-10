"""Lifecycle orchestration for Telegram bot startup and shutdown hooks."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.core.telethon_session import validate_and_repair_session as _validate_and_repair_session

if TYPE_CHECKING:
    from app.infrastructure.persistence.request_processing_job_repository import (
        InterruptedRequest,
    )

logger = get_logger(__name__)

# Bound on how long the post-startup notify task waits for the Telethon
# client to connect before giving up (best-effort; never blocks bot startup).
_INTERRUPTED_REQUEST_NOTIFY_CONNECT_TIMEOUT_SEC = 60.0


class TelegramLifecycleManager:
    """Handles startup warmup and background task lifecycle for TelegramBot."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._backup_task: asyncio.Task[None] | None = None
        self._rate_limiter_cleanup_task: asyncio.Task[None] | None = None
        self._interrupted_request_notify_task: asyncio.Task[None] | None = None

    @property
    def backup_task(self) -> asyncio.Task[None] | None:
        return self._backup_task

    @property
    def rate_limiter_cleanup_task(self) -> asyncio.Task[None] | None:
        return self._rate_limiter_cleanup_task

    async def on_startup(self) -> None:
        backup_enabled, interval, retention, backup_dir = self._bot._get_backup_settings()
        if backup_enabled and interval > 0:
            self._backup_task = asyncio.create_task(
                self._bot._run_backup_loop(interval, retention, backup_dir),
                name="db_backup_loop",
            )
        elif backup_enabled:
            logger.warning(
                "db_backup_disabled_invalid_interval",
                extra={"interval_minutes": interval},
            )

        self._rate_limiter_cleanup_task = asyncio.create_task(
            self._bot._run_rate_limiter_cleanup_loop(),
            name="rate_limiter_cleanup_loop",
        )

        await self._validate_digest_session()
        await self._warm_adaptive_timeout_cache()
        await self._clear_startup_cache()
        await self._recover_interrupted_synchronous_requests()

    async def on_shutdown(self) -> None:
        await self._cancel_task(self._backup_task)
        await self._cancel_task(self._rate_limiter_cleanup_task)
        await self._cancel_task(self._interrupted_request_notify_task)

    async def _validate_digest_session(self) -> None:
        cfg = getattr(self._bot, "cfg", None)
        if cfg is None or not getattr(getattr(cfg, "digest", None), "enabled", False):
            return

        session_name: str = cfg.digest.session_name
        session_file = Path("/data") / f"{session_name}.session"
        _validate_and_repair_session(session_file)

    async def _warm_adaptive_timeout_cache(self) -> None:
        adaptive_timeout = getattr(self._bot, "_adaptive_timeout_service", None)
        if adaptive_timeout is None:
            url_handler = getattr(self._bot.message_handler, "url_handler", None)
            if url_handler is not None:
                adaptive_timeout = getattr(url_handler, "_adaptive_timeout", None)

        if adaptive_timeout is None:
            return

        try:
            await adaptive_timeout.warm_cache()
            logger.info("adaptive_timeout_cache_warmed_on_startup")
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "adaptive_timeout_warmup_failed_on_startup",
                extra={"error": str(exc)},
            )

    async def _clear_startup_cache(self) -> None:
        try:
            cleaned = await self._bot.message_handler.url_handler.clear_extraction_cache()
            logger.info("startup_cache_cleared", extra={"count": cleaned})
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning("startup_cache_clear_failed", extra={"error": str(exc)})

    async def _recover_interrupted_synchronous_requests(self) -> None:
        """Detect bot-restart-orphaned synchronous requests; schedule owner notify.

        Called from ``on_startup``, which runs *before* the Telethon client
        connects (``TelegramBot.start()`` awaits ``on_startup()`` first, then
        calls ``telegram_client.start(...)`` -- which itself blocks in an idle
        loop for the remainder of the process). The DB detection and marking
        happens immediately here since the DB is available regardless of
        connection state; the actual Telegram send is deferred to a
        background task that waits for the client to connect, bounded by a
        timeout, so it never blocks or fails startup.
        """
        db = getattr(self._bot, "db", None)
        if db is None:
            return
        try:
            from app.infrastructure.persistence.request_processing_job_repository import (
                RequestProcessingJobRepository,
            )

            repo = RequestProcessingJobRepository(db)
            interrupted = await repo.recover_interrupted_synchronous_requests()
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "interrupted_request_recovery_failed",
                extra={"error": str(exc)},
            )
            return

        if not interrupted:
            return

        logger.info(
            "interrupted_requests_recovered",
            extra={"count": len(interrupted)},
        )
        self._interrupted_request_notify_task = asyncio.create_task(
            self._notify_interrupted_requests(interrupted),
            name="interrupted_request_notify",
        )

    async def _notify_interrupted_requests(self, interrupted: list[InterruptedRequest]) -> None:
        """Wait for the Telethon client to connect, then notify each owner chat.

        Best-effort: a failed send for one request never blocks the others,
        and a failure to connect within the timeout is logged and skipped.
        """
        try:
            if not await self._wait_for_telegram_client_connected(
                timeout_seconds=_INTERRUPTED_REQUEST_NOTIFY_CONNECT_TIMEOUT_SEC
            ):
                logger.warning(
                    "interrupted_request_notify_skipped_client_not_connected",
                    extra={"count": len(interrupted)},
                )
                return

            inner_client = self._bot.telegram_client.client
            for item in interrupted:
                text = _build_interrupted_request_message(item.input_url)
                try:
                    await inner_client.send_message(chat_id=item.chat_id, text=text)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning(
                        "interrupted_request_notify_send_failed",
                        extra={
                            "request_id": item.request_id,
                            "chat_id": item.chat_id,
                            "correlation_id": item.correlation_id,
                            "error": str(exc),
                        },
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("interrupted_request_notify_failed", extra={"error": str(exc)})

    async def _wait_for_telegram_client_connected(self, *, timeout_seconds: float) -> bool:
        """Poll the Telethon client's connection state, bounded by a timeout."""
        telegram_client = getattr(self._bot, "telegram_client", None)
        inner_client = getattr(telegram_client, "client", None)
        if inner_client is None:
            return False
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            if getattr(inner_client, "is_connected", False):
                return True
            await asyncio.sleep(0.5)
        return bool(getattr(inner_client, "is_connected", False))

    async def _cancel_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _build_interrupted_request_message(input_url: str | None) -> str:
    """Build the owner-facing notification text for an interrupted request."""
    if input_url:
        return (
            "Your earlier request could not be completed because the bot restarted "
            "while processing it. Please resend this link to try again:\n"
            f"{input_url}"
        )
    return (
        "Your earlier request could not be completed because the bot restarted "
        "while processing it. Please resend it to try again."
    )
