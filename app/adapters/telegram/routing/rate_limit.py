"""Rate limiting coordination for Telegram routing."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from app.core.ui_strings import t
from app.infrastructure.redis import get_redis
from app.security.rate_limiter import RateLimitConfig, RedisUserRateLimiter, UserRateLimiter

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.config import AppConfig

    from .interactions import MessageInteractionRecorder

logger = logging.getLogger("app.adapters.telegram.message_router")


class MessageRateLimitCoordinator:
    """Own rate limiter selection, state, and rejection handling."""

    def __init__(
        self,
        cfg: AppConfig,
        response_formatter: ResponseFormatter,
        interaction_recorder: MessageInteractionRecorder,
        *,
        lang: str = "en",
        recent_message_ttl: int = 120,
    ) -> None:
        self.cfg = cfg
        self.response_formatter = response_formatter
        self.interaction_recorder = interaction_recorder
        self._lang = lang

        self._rate_limiter_config = RateLimitConfig(
            max_requests=cfg.api_limits.requests_limit,
            window_seconds=cfg.api_limits.window_seconds,
            max_concurrent=cfg.api_limits.max_concurrent,
            cooldown_multiplier=cfg.api_limits.cooldown_multiplier,
        )
        self._rate_limiter = UserRateLimiter(self._rate_limiter_config)
        self._redis_limiter: RedisUserRateLimiter | None = None
        self._redis_limiter_available: bool | None = None
        self._rate_limit_notified_until: dict[int, float] = {}
        self._rate_limit_notice_window = max(self._rate_limiter_config.window_seconds, 30)
        self._recent_message_ids: dict[tuple[int, int, int], tuple[float, str]] = {}
        self._recent_message_ttl = recent_message_ttl

    @property
    def rate_limiter(self) -> UserRateLimiter:
        return self._rate_limiter

    @property
    def rate_limiter_config(self) -> RateLimitConfig:
        return self._rate_limiter_config

    @property
    def rate_limit_notified_until(self) -> dict[int, float]:
        return self._rate_limit_notified_until

    @property
    def recent_message_ids(self) -> dict[tuple[int, int, int], tuple[float, str]]:
        return self._recent_message_ids

    @property
    def recent_message_ttl(self) -> int:
        return self._recent_message_ttl

    async def get_active_limiter(self) -> RedisUserRateLimiter | UserRateLimiter:
        """Prefer Redis-backed rate limiting when Redis is available."""
        redis_client = await get_redis(self.cfg)

        if redis_client is not None:
            if self._redis_limiter is None:
                self._redis_limiter = RedisUserRateLimiter(
                    redis_client,
                    self._rate_limiter_config,
                    self.cfg.redis.prefix,
                )
                self._redis_limiter_available = True
                logger.info("telegram_rate_limiter_redis_enabled")
            return self._redis_limiter

        is_prod = self.cfg.deployment.is_production_mode
        if self._redis_limiter_available is True:
            if is_prod:
                logger.warning(
                    "telegram_rate_limiter_fallback_to_memory",
                    extra={
                        "warning": (
                            "[DEV-ONLY FALLBACK ACTIVE IN PRODUCTION] "
                            "Telegram rate limiting fell back to in-memory state. "
                            "Limits are not shared across workers or restarts."
                        )
                    },
                )
            else:
                logger.info("telegram_rate_limiter_fallback_to_memory")
        elif is_prod and self._redis_limiter_available is None:
            logger.warning(
                "telegram_rate_limiter_using_memory_in_production",
                extra={
                    "warning": (
                        "[DEV-ONLY FALLBACK ACTIVE IN PRODUCTION] "
                        "Redis unavailable at startup; Telegram rate limiting is "
                        "in-memory only. Limits are not shared across workers."
                    )
                },
            )
        self._redis_limiter_available = False
        return self._rate_limiter

    async def check_rate_limit(
        self,
        limiter: RedisUserRateLimiter | UserRateLimiter,
        uid: int,
        interaction_type: str,
    ) -> tuple[bool, str | None]:
        return await limiter.check_and_record(uid, operation=interaction_type)

    async def acquire_concurrent_slot(
        self,
        limiter: RedisUserRateLimiter | UserRateLimiter,
        uid: int,
    ) -> bool:
        return await limiter.acquire_concurrent_slot(uid)

    async def release_concurrent_slot(
        self,
        limiter: RedisUserRateLimiter | UserRateLimiter,
        uid: int,
    ) -> None:
        await limiter.release_concurrent_slot(uid)

    async def handle_rate_limit_rejection(
        self,
        *,
        message: object,
        uid: int,
        interaction_type: str,
        correlation_id: str,
        error_msg: str | None,
        interaction_id: int,
        start_time: float,
    ) -> None:
        logger.warning(
            "rate_limit_rejected",
            extra={"uid": uid, "interaction_type": interaction_type, "cid": correlation_id},
        )
        if error_msg and self._should_notify_rate_limit(uid):
            await self.response_formatter.safe_reply(message, error_msg)
        await self.interaction_recorder.update(
            interaction_id,
            response_sent=True,
            response_type="rate_limited",
            error_occurred=True,
            error_message="Rate limit exceeded",
            start_time=start_time,
        )

    async def handle_concurrent_limit_rejection(
        self,
        *,
        message: object,
        uid: int,
        interaction_type: str,
        correlation_id: str,
        interaction_id: int,
        start_time: float,
    ) -> None:
        logger.warning(
            "concurrent_limit_rejected",
            extra={"uid": uid, "interaction_type": interaction_type, "cid": correlation_id},
        )
        await self.response_formatter.safe_reply(message, t("concurrent_ops_limit", self._lang))
        await self.interaction_recorder.update(
            interaction_id,
            response_sent=True,
            response_type="concurrent_limited",
            error_occurred=True,
            error_message="Concurrent operations limit exceeded",
            start_time=start_time,
        )

    async def cleanup(self) -> int:
        """Clean up in-memory limiter and routing suppression state."""
        cleaned = await self._rate_limiter.cleanup_expired()

        now = time.time()
        expired_notifs = [
            uid for uid, deadline in self._rate_limit_notified_until.items() if now >= deadline
        ]
        for uid in expired_notifs:
            del self._rate_limit_notified_until[uid]

        cutoff = now - self._recent_message_ttl
        expired_msgs = [key for key, (ts, _sig) in self._recent_message_ids.items() if ts < cutoff]
        for key in expired_msgs:
            del self._recent_message_ids[key]

        return cleaned

    def _should_notify_rate_limit(self, uid: int) -> bool:
        now = time.time()
        deadline = self._rate_limit_notified_until.get(uid, 0.0)
        if now >= deadline:
            self._rate_limit_notified_until[uid] = now + self._rate_limit_notice_window
            return True

        logger.debug(
            "rate_limit_notice_suppressed",
            extra={
                "uid": uid,
                "remaining_suppression": max(0.0, deadline - now),
            },
        )
        return False
