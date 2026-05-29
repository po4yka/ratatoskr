import os
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import delete

from app.config.database import DatabaseConfig
from app.core.time_utils import UTC
from app.db.models import Request, Summary, User
from app.db.session import Database
from app.infrastructure.cache import trending_cache

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _test_dsn() -> str:
    return os.getenv("TEST_DATABASE_URL", "")


@pytest.fixture
async def database() -> "AsyncGenerator[Database]":
    dsn = _test_dsn()
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for Postgres repository tests")

    db = Database(DatabaseConfig(dsn=dsn, pool_size=1, max_overflow=1))
    await db.migrate()
    async with db.transaction() as session:
        await session.execute(delete(Summary))
        await session.execute(delete(Request))
        await session.execute(delete(User))
    try:
        yield db
    finally:
        async with db.transaction() as session:
            await session.execute(delete(Summary))
            await session.execute(delete(Request))
            await session.execute(delete(User))
        await db.dispose()


@pytest.mark.asyncio
async def test_trending_payload_is_cached(monkeypatch):
    trending_cache.clear_trending_cache()

    call_count = {"value": 0}

    async def fake_fetch(
        user_id: int,
        *,
        previous_period_start: datetime,
        max_scan: int,
        database=None,
    ):
        del database
        call_count["value"] += 1
        now = datetime.now(UTC)
        return [(now - timedelta(days=1), ["AI", "#AI", "ml"])]

    monkeypatch.setattr(trending_cache, "_fetch_trending_records", fake_fetch)
    monkeypatch.setattr(trending_cache, "TRENDING_CACHE_TTL_SECONDS", 60)

    first = await trending_cache.get_trending_payload(1, limit=5, days=30)
    second = await trending_cache.get_trending_payload(1, limit=5, days=30)

    assert call_count["value"] == 1
    assert first == second

    trending_cache.clear_trending_cache()


@pytest.mark.asyncio
async def test_trending_payload_prunes_expired_in_memory_entries(monkeypatch):
    trending_cache.clear_trending_cache()

    expired_key = (999, 1, 1)
    trending_cache._cache_manager._trending_cache[expired_key] = trending_cache.TrendingCacheEntry(
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
        payload={"tags": []},
    )

    async def fake_get_from_redis(user_id: int, days: int, limit: int):
        del user_id, days, limit

    async def fake_set_to_redis(
        user_id: int, days: int, limit: int, payload: dict[str, object]
    ) -> bool:
        del user_id, days, limit, payload
        return False

    async def fake_fetch(
        user_id: int,
        *,
        previous_period_start: datetime,
        max_scan: int,
        database=None,
    ):
        del user_id, previous_period_start, max_scan, database
        return []

    monkeypatch.setattr(trending_cache._cache_manager, "get_from_redis", fake_get_from_redis)
    monkeypatch.setattr(trending_cache._cache_manager, "set_to_redis", fake_set_to_redis)
    monkeypatch.setattr(trending_cache, "_fetch_trending_records", fake_fetch)

    await trending_cache.get_trending_payload(1, limit=5, days=30)

    assert expired_key not in trending_cache._cache_manager._trending_cache

    trending_cache.clear_trending_cache()


def test_build_trending_payload_uses_previous_period():
    now = datetime(2025, 1, 10, tzinfo=UTC)
    records = [
        (now - timedelta(days=2), ["ai", "AI"]),
        (now - timedelta(days=15), ["ai"]),
    ]

    payload = trending_cache._build_trending_payload(records, now=now, days=10, limit=5)

    assert payload["tags"][0]["tag"] == "ai"
    assert payload["tags"][0]["count"] == 2
    assert payload["tags"][0]["trend"] == "up"


@pytest.mark.asyncio
async def test_fetch_trending_records_uses_postgres_database(database: Database):
    now = datetime.now(UTC)
    async with database.transaction() as session:
        user = User(telegram_user_id=8901, username="trend-owner")
        session.add(user)
        await session.flush()
        request = Request(
            user_id=user.telegram_user_id,
            type="url",
            status="completed",
            input_url="https://example.com/trending",
            normalized_url="https://example.com/trending",
            dedupe_hash="trending-1",
            created_at=now - timedelta(days=1),
        )
        old_request = Request(
            user_id=user.telegram_user_id,
            type="url",
            status="completed",
            input_url="https://example.com/old",
            normalized_url="https://example.com/old",
            dedupe_hash="trending-2",
            created_at=now - timedelta(days=90),
        )
        session.add_all([request, old_request])
        await session.flush()
        session.add_all(
            [
                Summary(request_id=request.id, lang="en", json_payload={"topic_tags": ["AI"]}),
                Summary(request_id=old_request.id, lang="en", json_payload={"topic_tags": ["old"]}),
            ]
        )

    records = await trending_cache._fetch_trending_records(
        user.telegram_user_id,
        previous_period_start=now - timedelta(days=30),
        max_scan=10,
        database=database,
    )

    assert records == [(request.created_at, ["AI"])]


@pytest.mark.asyncio
async def test_clear_redis_uses_prefix_not_full_clear(monkeypatch):
    """C-2: _clear_redis must call clear_prefix("trending"), never clear()."""
    from app.infrastructure.cache.trending_cache import _TrendingCacheManager

    calls: list[tuple[str, tuple[str, ...]]] = []

    class _FakeRedisCache:
        async def clear(self) -> int:
            calls.append(("clear", ()))
            return 0

        async def clear_prefix(self, *parts: str) -> int:
            calls.append(("clear_prefix", parts))
            return 3

    manager = _TrendingCacheManager()
    manager._redis_cache = _FakeRedisCache()  # type: ignore[assignment]

    class _FakeCfg:
        pass

    manager._app_config = _FakeCfg()  # type: ignore[assignment]

    await manager._clear_redis()

    assert calls == [("clear_prefix", ("trending",))], (
        f"Expected clear_prefix('trending') only, got: {calls}"
    )
    assert not any(name == "clear" for name, _ in calls), "clear() must not be called"


@pytest.mark.asyncio
async def test_concurrent_misses_trigger_single_db_fetch(monkeypatch):
    """C-3: two concurrent misses on the same key must produce exactly one DB fetch."""
    import asyncio

    from app.infrastructure.cache import trending_cache

    trending_cache.clear_trending_cache()

    fetch_count = {"value": 0}

    async def fake_fetch(
        user_id: int,
        *,
        previous_period_start: datetime,
        max_scan: int,
        database=None,
    ):
        fetch_count["value"] += 1
        # Yield so the second coroutine has a chance to also call fetch if the
        # lock were released before the store (the old TOCTOU bug).
        await asyncio.sleep(0)
        return []

    async def fake_get_from_redis(user_id: int, days: int, limit: int):
        return None

    async def fake_set_to_redis(
        user_id: int, days: int, limit: int, payload: dict[str, object]
    ) -> bool:
        return False

    monkeypatch.setattr(trending_cache, "_fetch_trending_records", fake_fetch)
    monkeypatch.setattr(trending_cache._cache_manager, "get_from_redis", fake_get_from_redis)
    monkeypatch.setattr(trending_cache._cache_manager, "set_to_redis", fake_set_to_redis)
    monkeypatch.setattr(trending_cache, "TRENDING_CACHE_TTL_SECONDS", 60)

    # Launch two concurrent requests for the same key.
    results = await asyncio.gather(
        trending_cache.get_trending_payload(42, limit=5, days=7),
        trending_cache.get_trending_payload(42, limit=5, days=7),
    )

    assert fetch_count["value"] == 1, (
        f"Expected exactly 1 DB fetch for concurrent misses, got {fetch_count['value']}"
    )
    # Both callers must receive the same payload.
    assert results[0] == results[1]

    trending_cache.clear_trending_cache()
