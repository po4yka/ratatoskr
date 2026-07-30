from __future__ import annotations

import asyncio
import inspect
import time
from typing import TYPE_CHECKING

import redis.asyncio as aioredis

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.config import AppConfig, RedisConfig

logger = get_logger(__name__)


class _RedisConnectionManager:
    """Encapsulates process-wide Redis connection state and reconnection logic."""

    def __init__(self) -> None:
        self.client: aioredis.Redis | None = None
        self.lock = asyncio.Lock()
        # The loop the cached client and lock were built on. A redis.asyncio
        # pool keeps futures bound to its creating loop, and an asyncio.Lock
        # binds on first acquire, so neither survives being reused from another
        # one -- the symptom is "got Future attached to a different loop" from
        # somewhere entirely unrelated, such as a rate-limit middleware call.
        self._loop: asyncio.AbstractEventLoop | None = None
        self.last_connection_attempt: float = 0.0
        self.last_error: str | None = None
        self.connection_failed: bool = False

    async def get(self, cfg: AppConfig) -> aioredis.Redis | None:
        """Get or create a shared Redis client.

        Returns None when Redis is disabled or unavailable and not required.
        Raises the connection error when required=True.

        Implements reconnection logic with rate-limiting:
        - If connection failed previously, waits for reconnect_interval before retrying
        - Logs at INFO level (not WARNING) when Redis is optional and unavailable
        - Only logs full traceback when REDIS_REQUIRED=true
        """
        if not cfg.redis.enabled:
            return None

        running = asyncio.get_running_loop()
        if self._loop is not running:
            # Drop rather than close: awaiting aclose() on a pool owned by
            # another loop raises the very error being avoided. A loop swap is
            # rare (a test harness, or sequential asyncio.run calls in one
            # process), so the leaked pool is cheaper than the crash.
            self.client = None
            self.lock = asyncio.Lock()
            self._loop = running

        async with self.lock:
            if self.client is not None:
                return self.client

            now = time.time()
            reconnect_interval = cfg.redis.reconnect_interval
            if self.connection_failed and reconnect_interval > 0:
                elapsed = now - self.last_connection_attempt
                if elapsed < reconnect_interval:
                    return None

            self.last_connection_attempt = now
            url = _build_url(cfg.redis)

            try:
                self.client = aioredis.from_url(
                    url,
                    password=cfg.redis.password,
                    socket_timeout=cfg.redis.socket_timeout,
                    decode_responses=True,
                )
                ping_result = self.client.ping()
                if inspect.isawaitable(ping_result):
                    await ping_result

                event = "redis_reconnected" if self.connection_failed else "redis_connected"
                logger.info(
                    event,
                    extra={"url": url, "db": cfg.redis.db, "prefix": cfg.redis.prefix},
                )

                self.connection_failed = False
                self.last_error = None
                return self.client

            except Exception as exc:
                self.client = None
                self.connection_failed = True
                self.last_error = str(exc)

                if cfg.redis.required:
                    logger.error(
                        "redis_connection_failed",
                        exc_info=True,
                        extra={"url": url, "db": cfg.redis.db, "required": True},
                    )
                    raise

                logger.info("redis_unavailable", extra={"url": url, "error": self.last_error})
                return None

    def connection_state(self) -> dict[str, bool | float | str | None]:
        return {
            "connected": self.client is not None,
            "last_attempt": self.last_connection_attempt,
            "last_error": self.last_error,
            "connection_failed": self.connection_failed,
        }

    async def close(self) -> None:
        if self.client:
            try:
                await self.client.aclose()
                logger.info("redis_closed")
            finally:
                self.client = None
                self._loop = None
                self.connection_failed = False
                self.last_error = None


_manager = _RedisConnectionManager()


def _build_url(cfg: RedisConfig) -> str:
    if cfg.url:
        return cfg.url
    return f"redis://{cfg.host}:{cfg.port}/{cfg.db}"


def redis_key(prefix: str, *parts: str) -> str:
    """Compose a namespaced Redis key."""
    safe_parts = [part for part in parts if part]
    return ":".join([prefix, *safe_parts])


async def get_redis(cfg: AppConfig) -> aioredis.Redis | None:
    """Get or create the shared Redis client."""
    return await _manager.get(cfg)


def get_connection_state() -> dict[str, bool | float | str | None]:
    """Get current Redis connection state for health checks.

    Returns:
        Dict with keys:
        - connected: bool - whether client is connected
        - last_attempt: float - unix timestamp of last connection attempt (0 if never)
        - last_error: str | None - last error message if connection failed
        - connection_failed: bool - whether last connection attempt failed
    """
    return _manager.connection_state()


async def close_redis() -> None:
    """Close shared Redis client if present."""
    await _manager.close()
