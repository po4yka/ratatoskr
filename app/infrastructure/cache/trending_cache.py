"""Shared trending topics cache utilities.

Supports both Redis (shared across workers) and in-memory (fallback) caching.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.logging_utils import get_logger
from app.core.time_utils import UTC
from app.db.models import Request as RequestModel, Summary

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.db.session import Database
    from app.infrastructure.cache.redis_cache import RedisCache

logger = get_logger(__name__)

TRENDING_CACHE_TTL_SECONDS = 300
TRENDING_MAX_SCAN = 1000


@dataclass(slots=True)
class TrendingCacheEntry:
    expires_at: datetime
    payload: dict[str, Any]


class _TrendingCacheManager:
    """Encapsulates in-memory and Redis cache state for trending topics."""

    def __init__(self) -> None:
        self._trending_cache: dict[tuple[int, int, int], TrendingCacheEntry] = {}
        self._lock = asyncio.Lock()
        self._redis_cache: RedisCache | None = None
        self._app_config: AppConfig | None = None

    def prune_expired(self, now: datetime) -> int:
        """Remove expired fallback cache entries and return the number deleted."""
        expired_keys = [
            key for key, entry in self._trending_cache.items() if entry.expires_at <= now
        ]
        for key in expired_keys:
            self._trending_cache.pop(key, None)
        return len(expired_keys)

    def get_redis_cache(self) -> tuple[RedisCache | None, AppConfig | None]:
        """Get or initialize the Redis cache singleton."""
        if self._redis_cache is not None:
            return self._redis_cache, self._app_config

        try:
            from app.config import load_config
            from app.infrastructure.cache.redis_cache import RedisCache

            self._app_config = load_config(allow_stub_telegram=True)
            if not self._app_config.redis.enabled:
                return None, self._app_config

            self._redis_cache = RedisCache(self._app_config)
            return self._redis_cache, self._app_config
        except Exception as exc:
            logger.debug("trending_redis_cache_init_skipped", extra={"error": str(exc)})
            return None, None

    async def get_from_redis(self, user_id: int, days: int, limit: int) -> dict[str, Any] | None:
        """Try to get trending payload from Redis cache."""
        redis_cache, cfg = self.get_redis_cache()
        if redis_cache is None or cfg is None:
            return None

        try:
            cached = await redis_cache.get_json("trending", str(user_id), str(days), str(limit))
            if isinstance(cached, dict):
                logger.debug(
                    "trending_redis_cache_hit",
                    extra={"user_id": user_id, "days": days, "limit": limit},
                )
                return cached
        except Exception as exc:
            logger.warning(
                "trending_redis_cache_get_failed",
                extra={"user_id": user_id, "error": str(exc)},
            )
        return None

    async def set_to_redis(
        self, user_id: int, days: int, limit: int, payload: dict[str, Any]
    ) -> bool:
        """Store trending payload in Redis cache."""
        redis_cache, cfg = self.get_redis_cache()
        if redis_cache is None or cfg is None:
            return False

        try:
            ttl = cfg.redis.trending_cache_ttl_seconds
            success = await redis_cache.set_json(
                value=payload,
                ttl_seconds=ttl,
                parts=("trending", str(user_id), str(days), str(limit)),
            )
            if success:
                logger.debug(
                    "trending_redis_cached",
                    extra={"user_id": user_id, "days": days, "limit": limit, "ttl": ttl},
                )
            return success
        except Exception as exc:
            logger.warning(
                "trending_redis_cache_set_failed",
                extra={"user_id": user_id, "error": str(exc)},
            )
            return False

    async def get_payload(
        self,
        user_id: int,
        *,
        limit: int,
        days: int,
        database: Database | None = None,
    ) -> dict[str, Any]:
        """Return trending topics with per-user/param caching.

        Singleflight: the asyncio lock is held across the DB fetch + store so
        that concurrent misses on the same key cause only one DB scan. A
        coroutine that was waiting on the lock performs a double-checked lookup
        after acquiring it and returns the winner's result without re-querying.
        Fail-open: any exception during fetch/store releases the lock via
        ``async with`` and propagates normally.
        """
        now = datetime.now(UTC)

        redis_cached = await self.get_from_redis(user_id, days, limit)
        if redis_cached is not None:
            return redis_cached

        cache_key = (user_id, limit, days)
        async with self._lock:
            # Double-checked lookup: a coroutine that waited on the lock may
            # find the value already populated by the winner.
            self.prune_expired(now)
            cached = self._trending_cache.get(cache_key)
            if cached and cached.expires_at > now:
                return cached.payload

            # Still a miss while holding the lock — this coroutine is the
            # winner and performs the DB fetch under the lock (singleflight).
            previous_period_start = now - timedelta(days=days * 2)
            max_scan = min(TRENDING_MAX_SCAN, max(limit * 40, 400))

            records = await _fetch_trending_records(
                user_id,
                previous_period_start=previous_period_start,
                max_scan=max_scan,
                database=database,
            )

            payload = _build_trending_payload(records, now=now, days=days, limit=limit)

            await self.set_to_redis(user_id, days, limit, payload)

            self._trending_cache[cache_key] = TrendingCacheEntry(
                expires_at=now + timedelta(seconds=TRENDING_CACHE_TTL_SECONDS),
                payload=payload,
            )

        return payload

    def clear(self) -> None:
        """Clear cached trending results (e.g., after summary writes)."""
        self._trending_cache.clear()

        redis_cache, cfg = self.get_redis_cache()
        if redis_cache is not None and cfg is not None:
            try:
                asyncio.get_event_loop().create_task(self._clear_redis())
            except RuntimeError as exc:
                logger.debug("trending_redis_clear_deferred", extra={"error": str(exc)})

    async def _clear_redis(self) -> None:
        """Clear only trending-prefixed entries from Redis cache.

        Uses ``clear_prefix("trending")`` so that unrelated keys (auth tokens,
        embeddings, query results, batch progress) are not evicted.
        The trending cache writes keys under the ``trending`` sub-prefix via
        ``set_json(..., parts=("trending", user_id, days, limit))``, which
        produces keys of the form ``ratatoskr:trending:<user_id>:<days>:<limit>``.
        ``clear_prefix("trending")`` matches ``ratatoskr:trending:*`` exactly.
        """
        redis_cache, cfg = self.get_redis_cache()
        if redis_cache is None or cfg is None:
            return

        try:
            deleted = await redis_cache.clear_prefix("trending")
            logger.debug("trending_redis_cache_cleared", extra={"deleted_count": deleted})
        except Exception as exc:
            logger.warning("trending_redis_cache_clear_failed", extra={"error": str(exc)})


_cache_manager = _TrendingCacheManager()


def _normalize_tag(tag: Any) -> str | None:
    if tag is None:
        return None
    text = str(tag).strip()
    if not text:
        return None
    return text.lower()


async def _fetch_trending_records(
    user_id: int,
    *,
    previous_period_start: datetime,
    max_scan: int,
    database: Database,
) -> list[tuple[datetime, list[str]]]:
    """Fetch recent summaries with tags for trending computation.

    `database` is required: infrastructure modules must not depend on
    the DI layer. The caller (api/routers/search.py) is responsible
    for supplying the runtime Database via FastAPI dependency
    injection.
    """
    records: list[tuple[datetime, list[str]]] = []
    async with database.session() as session:
        rows = (
            await session.execute(
                select(Summary.json_payload, RequestModel.created_at)
                .join(RequestModel, Summary.request_id == RequestModel.id)
                .where(
                    RequestModel.user_id == user_id,
                    RequestModel.created_at >= previous_period_start,
                )
                .order_by(RequestModel.created_at.desc())
                .limit(max_scan)
            )
        ).all()

    for payload, created_at in rows:
        payload = payload or {}
        topic_tags = payload.get("topic_tags") or []
        tag_list = topic_tags if isinstance(topic_tags, list) else []
        if created_at:
            records.append((created_at, tag_list))

    return records


def _build_trending_payload(
    records: list[tuple[datetime, list[str]]],
    *,
    now: datetime,
    days: int,
    limit: int,
) -> dict[str, Any]:
    current_period_start = now - timedelta(days=days)
    previous_period_start = current_period_start - timedelta(days=days)

    current_tags: Counter[str] = Counter()
    previous_tags: Counter[str] = Counter()

    for created_at, raw_tags in records:
        if not created_at:
            continue

        normalized_tags = [_normalize_tag(tag) for tag in raw_tags]
        normalized_tags = [tag for tag in normalized_tags if tag]
        if not normalized_tags:
            continue

        if created_at >= current_period_start:
            current_tags.update(normalized_tags)
        elif created_at >= previous_period_start:
            previous_tags.update(normalized_tags)

    trending_tags = []
    for tag, count in current_tags.most_common(limit):
        prev_count = previous_tags.get(tag, 0)

        if prev_count > 0:
            percentage_change = ((count - prev_count) / prev_count) * 100
        else:
            percentage_change = 100.0 if count > 0 else 0.0

        if percentage_change > 10:
            trend = "up"
        elif percentage_change < -10:
            trend = "down"
        else:
            trend = "stable"

        trending_tags.append(
            {
                "tag": tag,
                "count": count,
                "trend": trend,
                "percentage_change": round(percentage_change, 1),
            }
        )

    return {
        "tags": trending_tags,
        "time_range": {
            "start": current_period_start.isoformat().replace("+00:00", "Z"),
            "end": now.isoformat().replace("+00:00", "Z"),
        },
    }


async def get_trending_payload(
    user_id: int,
    *,
    limit: int,
    days: int,
    database: Database | None = None,
) -> dict[str, Any]:
    """Return trending topics with per-user/param caching.

    Uses Redis cache if available, falls back to in-memory cache otherwise.
    """
    return await _cache_manager.get_payload(user_id, limit=limit, days=days, database=database)


def clear_trending_cache() -> None:
    """Clear cached trending results (e.g., after summary writes).

    Clears both in-memory and Redis caches.
    """
    _cache_manager.clear()
