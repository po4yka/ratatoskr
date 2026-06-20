"""Session storage collaborators for sync flows."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, cast

from app.core.json_utils import dumps as json_dumps, loads as json_loads
from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.infrastructure.redis import get_redis, redis_key

logger = get_logger(__name__)


def parse_session_expires_at(payload: dict[str, Any]) -> datetime | None:
    expires_raw = payload.get("expires_at")
    if not isinstance(expires_raw, str) or not expires_raw:
        return None
    try:
        return datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
    except ValueError:
        return None


class SyncSessionStorePort(Protocol):
    async def store(self, payload: dict[str, Any], *, ttl_seconds: int) -> None: ...

    async def load(self, session_id: str) -> dict[str, Any] | None: ...

    async def delete(self, session_id: str) -> None: ...


class InMemorySyncSessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    def prune(self, now: datetime, *, exclude_session_id: str | None = None) -> int:
        expired_session_ids = []
        for session_id, payload in self._sessions.items():
            if exclude_session_id is not None and session_id == exclude_session_id:
                continue
            expires_at = parse_session_expires_at(payload)
            if expires_at is not None and now >= expires_at:
                expired_session_ids.append(session_id)

        for session_id in expired_session_ids:
            self._sessions.pop(session_id, None)
        return len(expired_session_ids)

    async def store(self, payload: dict[str, Any], *, ttl_seconds: int) -> None:
        _ = ttl_seconds
        self.prune(datetime.now(UTC))
        self._sessions[payload["session_id"]] = payload

    async def load(self, session_id: str) -> dict[str, Any] | None:
        self.prune(datetime.now(UTC), exclude_session_id=session_id)
        return self._sessions.get(session_id)

    async def delete(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


class RedisSyncSessionStore:
    def __init__(self, cfg: Any, *, get_redis_func: Any = get_redis) -> None:
        self._cfg = cfg
        self._get_redis = get_redis_func

    async def store(self, payload: dict[str, Any], *, ttl_seconds: int) -> None:
        redis_client = await self._get_redis(self._cfg)
        if redis_client is None:
            raise RuntimeError("Redis client unavailable")
        key = redis_key(self._cfg.redis.prefix, "sync", "session", payload["session_id"])
        await redis_client.set(key, json_dumps(payload), ex=ttl_seconds)

    async def load(self, session_id: str) -> dict[str, Any] | None:
        redis_client = await self._get_redis(self._cfg)
        if redis_client is None:
            raise RuntimeError("Redis client unavailable")
        key = redis_key(self._cfg.redis.prefix, "sync", "session", session_id)
        payload_raw = await redis_client.get(key)
        ttl = await redis_client.ttl(key)
        if payload_raw is None or ttl == -2:
            return None
        return cast("dict[str, Any] | None", json_loads(payload_raw))

    async def delete(self, session_id: str) -> None:
        redis_client = await self._get_redis(self._cfg)
        if redis_client is None:
            return
        key = redis_key(self._cfg.redis.prefix, "sync", "session", session_id)
        await redis_client.delete(key)


class FallbackSyncSessionStore:
    """Session store that attempts Redis first and falls back to in-memory.

    DATA-LOSS WARNING: when Redis is unavailable and writes fall back to
    in-memory storage, session data is lost on process restart and is not
    visible to other process replicas.  This fallback is intended only as a
    short-lived safety net during transient Redis outages; it is not a
    durable replacement for Redis.
    """

    def __init__(
        self,
        *,
        redis_store: RedisSyncSessionStore,
        fallback_store: InMemorySyncSessionStore,
    ) -> None:
        self._redis_store = redis_store
        self._fallback_store = fallback_store

    async def store(self, payload: dict[str, Any], *, ttl_seconds: int) -> None:
        try:
            await self._redis_store.store(payload, ttl_seconds=ttl_seconds)
            return
        except Exception as exc:
            # Log every Redis write failure so operators can detect persistent
            # outages.  Data written to the in-memory fallback is process-local
            # and will be lost on restart — see class docstring.
            logger.warning(
                "sync_session_redis_write_failed_using_memory_fallback",
                session_id=payload.get("session_id"),
                error=str(exc),
            )
        await self._fallback_store.store(payload, ttl_seconds=ttl_seconds)

    async def load(self, session_id: str) -> dict[str, Any] | None:
        try:
            payload = await self._redis_store.load(session_id)
        except Exception as exc:
            logger.warning(
                "sync_session_redis_load_failed_using_memory_fallback",
                session_id=session_id,
                error=str(exc),
            )
            payload = None
        if payload is not None:
            return payload
        return await self._fallback_store.load(session_id)

    async def delete(self, session_id: str) -> None:
        try:
            await self._redis_store.delete(session_id)
        except Exception as exc:
            logger.warning(
                "sync_session_redis_delete_failed",
                session_id=session_id,
                error=str(exc),
            )
        await self._fallback_store.delete(session_id)
