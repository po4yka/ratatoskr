"""Lifecycle orchestration for Telegram bot startup and shutdown hooks."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.core.telethon_session import validate_and_repair_session as _validate_and_repair_session

logger = get_logger(__name__)


class TelegramLifecycleManager:
    """Handles startup warmup and background task lifecycle for TelegramBot."""

    def __init__(self, bot: Any) -> None:
        self._bot = bot
        self._backup_task: asyncio.Task[None] | None = None
        self._rate_limiter_cleanup_task: asyncio.Task[None] | None = None

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

    async def on_shutdown(self) -> None:
        await self._cancel_task(self._backup_task)
        await self._cancel_task(self._rate_limiter_cleanup_task)

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

    async def _cancel_task(self, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
