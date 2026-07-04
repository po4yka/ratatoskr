"""Redis-backed distributed lock using SET NX EX + Lua atomic release."""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING
from uuid import uuid4

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    import redis.asyncio as aioredis

logger = get_logger(__name__)

# Lua script: delete the key only when its value matches our token.
# Returns 1 if deleted, 0 if the key was gone or held by a different token.
_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

# Lua script: refresh the key's TTL only when its value still matches our
# token (compare-and-expire). Returns 1 if refreshed, 0 if the key was gone
# or had already been re-acquired by a different owner.
_REFRESH_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("PEXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""

# Refresh the TTL at roughly this fraction of it so a heartbeat cannot be
# starved by scheduling jitter before the key expires.
_HEARTBEAT_FRACTION = 3


class RedisDistributedLock:
    """Async context manager that acquires a Redis-backed distributed lock.

    Usage::

        async with RedisDistributedLock(redis_client, "task_lock:my_task", ttl_seconds=1800) as acquired:
            if not acquired:
                return  # another worker holds the lock
            # ... do work ...

    The lock is acquired via ``SET key token NX EX ttl_seconds``.  While held,
    a background heartbeat refreshes the TTL roughly every ``ttl / 3``
    seconds via a compare-and-expire Lua script, so a task that runs longer
    than the original TTL keeps its lock instead of losing it to a second
    scheduled run. Release uses an atomic Lua script (check-and-delete) so a
    slow task that outlives its TTL cannot accidentally evict a newer
    holder's lock.

    If *redis_client* is ``None`` (Redis disabled), acquisition always
    succeeds so callers behave correctly in single-worker environments.
    """

    def __init__(
        self,
        redis_client: aioredis.Redis | None,
        key: str,
        ttl_seconds: int,
    ) -> None:
        self._client = redis_client
        self._key = key
        self._ttl = ttl_seconds
        self._token: str | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> bool:
        if self._client is None:
            # Redis unavailable — act as an uncontested lock so the task runs.
            self._token = str(uuid4())
            return True

        self._token = str(uuid4())
        try:
            result = await self._client.set(
                self._key,
                self._token,
                nx=True,
                ex=self._ttl,
            )
        except Exception as exc:
            # Fail-open: if Redis is unreachable, let the task proceed rather
            # than silently dropping it.
            logger.warning(
                "redis_lock_acquire_error",
                extra={"key": self._key, "error": str(exc)},
            )
            return True

        acquired = result is not None  # SET NX returns None when key exists
        if not acquired:
            logger.warning(
                "lock_held_by_other_worker",
                extra={"key": self._key, "ttl": self._ttl},
            )
            self._token = None
            return False

        self._heartbeat_task = asyncio.ensure_future(self._heartbeat())
        return True

    async def _heartbeat(self) -> None:
        """Periodically refresh the lock's TTL while it is held.

        Uses a compare-and-expire Lua script so the refresh only extends the
        key when this owner's token is still the one stored — a lock that a
        different owner has since re-acquired (because our TTL already
        lapsed) is never extended.
        """
        assert self._client is not None
        assert self._token is not None
        interval = max(self._ttl / _HEARTBEAT_FRACTION, 1)
        ttl_ms = self._ttl * 1000

        while True:
            await asyncio.sleep(interval)
            try:
                result = self._client.eval(
                    _REFRESH_SCRIPT, 1, self._key, self._token, ttl_ms
                )
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                # Transient Redis error: keep trying on the next tick rather
                # than giving up the heartbeat outright.
                logger.warning(
                    "redis_lock_heartbeat_error",
                    extra={"key": self._key, "error": str(exc)},
                )
                continue

            if result != 1:
                # Another owner holds the key now (our TTL already lapsed) —
                # further refreshes would extend someone else's lock.
                logger.warning(
                    "redis_lock_heartbeat_lost",
                    extra={"key": self._key},
                )
                return

    async def __aexit__(self, *args: object) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        if self._client is None or self._token is None:
            return

        try:
            result = self._client.eval(_RELEASE_SCRIPT, 1, self._key, self._token)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            # Non-fatal: TTL will expire the key automatically.
            logger.warning(
                "redis_lock_release_error",
                extra={"key": self._key, "error": str(exc)},
            )
        finally:
            self._token = None
