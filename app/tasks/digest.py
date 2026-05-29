"""Taskiq task: scheduled channel digest delivery."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.tasks.broker import broker
from app.tasks.deps import (
    create_digest_bot_client,
    create_digest_llm_client,
    create_digest_service,
    create_digest_userbot,
    get_app_config,
)

logger = get_logger(__name__)

_LOCK_KEY = "ratatoskr:digest:scheduled:lock"
_LOCK_TTL_MS = 10 * 60 * 1000  # 10 minutes — exceeds expected max digest duration


@asynccontextmanager
async def _acquire_scheduled_lock(cfg: AppConfig, correlation_id: str) -> AsyncGenerator[bool]:
    """Acquire a Redis distributed lock for scheduled digest runs.

    Yields True when this instance should proceed (lock acquired, or Redis
    unavailable → graceful degrade).  Yields False when another instance
    already holds the lock — caller must skip this run.
    """
    from app.infrastructure.redis import get_redis

    redis_client = None
    lock_acquired = False

    try:
        redis_client = await get_redis(cfg)
    except Exception:
        redis_client = None

    if redis_client is None:
        logger.warning("digest_lock_redis_unavailable", extra={"cid": correlation_id})
        yield True
        return

    try:
        result = await redis_client.set(_LOCK_KEY, correlation_id, nx=True, px=_LOCK_TTL_MS)
        lock_acquired = bool(result)
        if not lock_acquired:
            logger.warning("digest_lock_held_skipping", extra={"cid": correlation_id})
        yield lock_acquired
    finally:
        if lock_acquired:
            try:
                current = await redis_client.get(_LOCK_KEY)
                if current == correlation_id:
                    await redis_client.delete(_LOCK_KEY)
            except Exception:
                pass


@broker.task(task_name="ratatoskr.digest.run")
async def run_channel_digest(cfg: AppConfig = TaskiqDepends(get_app_config)) -> None:
    """Execute scheduled channel digest delivery for all subscribed users."""
    await _channel_digest_body(cfg)


async def _channel_digest_body(cfg: AppConfig) -> None:
    """Core digest logic — separated for direct testability."""
    correlation_id = f"digest_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    logger.info("scheduled_digest_starting", extra={"cid": correlation_id})

    async with _acquire_scheduled_lock(cfg, correlation_id) as lock_acquired:
        if not lock_acquired:
            return

        userbot: Any | None = None
        llm_client: Any | None = None
        try:
            userbot = create_digest_userbot(cfg)
            await userbot.start()

            llm_client = create_digest_llm_client(cfg)
            bot = create_digest_bot_client(cfg)

            async with bot:

                async def send_message(user_id: int, text: str, reply_markup: Any = None) -> None:
                    await bot.send_message(chat_id=user_id, text=text, reply_markup=reply_markup)

                service = create_digest_service(
                    cfg,
                    userbot=userbot,
                    llm_client=llm_client,
                    send_message=send_message,
                )

                user_ids = await service.async_get_users_with_subscriptions()
                logger.info(
                    "scheduled_digest_users",
                    extra={"cid": correlation_id, "count": len(user_ids)},
                )

                for uid in user_ids:
                    try:
                        result = await service.generate_digest(
                            user_id=uid,
                            correlation_id=f"{correlation_id}_u{uid}",
                            digest_type="scheduled",
                            lang="ru",
                        )
                        logger.info(
                            "scheduled_digest_user_complete",
                            extra={
                                "cid": correlation_id,
                                "uid": uid,
                                "posts": result.post_count,
                                "errors": len(result.errors),
                            },
                        )
                    except Exception as exc:
                        logger.exception(
                            "scheduled_digest_user_failed",
                            extra={"cid": correlation_id, "uid": uid, "error": str(exc)},
                        )

        except Exception as exc:
            logger.exception(
                "scheduled_digest_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
        finally:
            if llm_client is not None:
                await llm_client.aclose()
            if userbot is not None:
                await userbot.stop()
