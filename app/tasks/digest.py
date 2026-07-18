"""Taskiq task: scheduled channel digest delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.observability.metrics_digest import (
    record_digest_delivery,
    set_digest_active_subscription_users,
)
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
# Base TTL (seconds). RedisDistributedLock renews it via a background heartbeat
# (~every ttl/3) while the run is in progress, so a digest that outlives the TTL
# keeps its lock instead of losing it to a second scheduled run — matching every
# other task's lock. Release is an atomic compare-and-delete (no GET/DELETE race).
_LOCK_TTL_SECONDS = 10 * 60


@broker.task(task_name="ratatoskr.digest.run")
async def run_channel_digest(cfg: AppConfig = TaskiqDepends(get_app_config)) -> None:
    """Execute scheduled channel digest delivery for all subscribed users."""
    await _channel_digest_body(cfg)


async def _channel_digest_body(cfg: AppConfig) -> None:
    """Core digest logic — separated for direct testability."""
    correlation_id = f"digest_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
    logger.info("scheduled_digest_starting", extra={"cid": correlation_id})

    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(redis_client, _LOCK_KEY, _LOCK_TTL_SECONDS) as lock_acquired:
        if not lock_acquired:
            logger.warning("digest_lock_held_skipping", extra={"cid": correlation_id})
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
                set_digest_active_subscription_users(len(user_ids))
                logger.info(
                    "scheduled_digest_users",
                    extra={"cid": correlation_id, "count": len(user_ids)},
                )

                for uid in user_ids:
                    try:
                        lang = await service.async_get_user_locale(uid)
                        result = await service.generate_digest(
                            user_id=uid,
                            correlation_id=f"{correlation_id}_u{uid}",
                            digest_type="scheduled",
                            lang=lang,
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
                        record_digest_delivery("failed")

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
