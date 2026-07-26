"""ChronicFailureMiddleware must track streaks in shared Redis, not per-process.

A single task's failures are round-robined across N worker processes, and
workers restart. A per-process counter therefore under-counts (each process sees
~1/N of the failures) and resets on restart, so the chronic-failure metric would
under-fire. These tests exercise the middleware against a shared in-memory fake
Redis (no server, no network) plus the in-process fallback.
"""

from __future__ import annotations

from typing import Any

import pytest

import app.infrastructure.redis as redis_module
import app.tasks.middleware as middleware_module
from app.tasks.middleware import (
    _CHRONIC_FAILURE_THRESHOLD,
    _CHRONIC_FAILURE_TTL_SEC,
    ChronicFailureMiddleware,
)

pytestmark = pytest.mark.no_network


class _FakeMessage:
    def __init__(self, task_name: str) -> None:
        self.task_name = task_name


class _FakeResult:
    def __init__(self, *, is_err: bool, error: Any = None) -> None:
        self.is_err = is_err
        self.error = error


class _FakeRedis:
    """Shared in-memory Redis stand-in (one instance = one cluster)."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self.expires[key] = ttl
        return True

    async def delete(self, key: str) -> int:
        existed = key in self.store
        self.store.pop(key, None)
        self.expires.pop(key, None)
        return 1 if existed else 0


def _make_middleware() -> ChronicFailureMiddleware:
    mw = ChronicFailureMiddleware()
    # Skip load_config; the fake client is injected via the get_redis patch.
    mw._cfg = object()
    return mw


def _key(task: str) -> str:
    return f"taskiq:chronic_failures:{task}"


@pytest.fixture
def record_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture record_scheduler_chronic_failure calls (works without prometheus)."""
    calls: list[str] = []
    monkeypatch.setattr(
        middleware_module,
        "record_scheduler_chronic_failure",
        lambda task_name: calls.append(task_name),
    )
    return calls


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()

    async def _get_redis(_cfg: Any) -> _FakeRedis:
        return fake

    monkeypatch.setattr(redis_module, "get_redis", _get_redis)
    return fake


@pytest.mark.asyncio
async def test_streak_aggregates_across_worker_instances(
    fake_redis: _FakeRedis, record_spy: list[str]
) -> None:
    task = "ratatoskr.chronic.cross_process"

    mw_a = _make_middleware()
    mw_b = _make_middleware()

    # Failures round-robin across two independent "processes" sharing one Redis.
    await mw_a.post_execute(_FakeMessage(task), _FakeResult(is_err=True))
    await mw_b.post_execute(_FakeMessage(task), _FakeResult(is_err=True))
    # Two failures: below threshold, so the metric must not have fired. A
    # per-process counter would sit at 1 in each and never reach the threshold.
    assert record_spy == []

    await mw_a.post_execute(_FakeMessage(task), _FakeResult(is_err=True))
    # Global streak is now 3 -> metric fires on the crossing.
    assert record_spy == [task]
    assert fake_redis.store[_key(task)] == 3


@pytest.mark.asyncio
async def test_streak_survives_worker_restart(
    fake_redis: _FakeRedis, record_spy: list[str]
) -> None:
    task = "ratatoskr.chronic.restart"

    mw_old = _make_middleware()
    await mw_old.post_execute(_FakeMessage(task), _FakeResult(is_err=True))
    await mw_old.post_execute(_FakeMessage(task), _FakeResult(is_err=True))

    # A restart drops the in-process dict; a fresh instance keeps the Redis streak.
    mw_new = _make_middleware()
    await mw_new.post_execute(_FakeMessage(task), _FakeResult(is_err=True))

    assert record_spy == [task]
    assert fake_redis.store[_key(task)] == 3


@pytest.mark.asyncio
async def test_success_clears_shared_streak(fake_redis: _FakeRedis) -> None:
    task = "ratatoskr.chronic.recover"
    mw = _make_middleware()

    await mw.post_execute(_FakeMessage(task), _FakeResult(is_err=True))
    await mw.post_execute(_FakeMessage(task), _FakeResult(is_err=True))
    assert fake_redis.store[_key(task)] == 2

    # Success wipes the shared key for every worker at once.
    await mw.post_execute(_FakeMessage(task), _FakeResult(is_err=False))
    assert _key(task) not in fake_redis.store

    # The next failure starts a fresh streak.
    await mw.post_execute(_FakeMessage(task), _FakeResult(is_err=True))
    assert fake_redis.store[_key(task)] == 1


@pytest.mark.asyncio
async def test_ttl_refreshed_on_each_failure(fake_redis: _FakeRedis) -> None:
    task = "ratatoskr.chronic.ttl"
    mw = _make_middleware()
    await mw.post_execute(_FakeMessage(task), _FakeResult(is_err=True))
    assert fake_redis.expires[_key(task)] == _CHRONIC_FAILURE_TTL_SEC


@pytest.mark.asyncio
async def test_falls_back_to_in_process_counter_when_redis_unavailable(
    monkeypatch: pytest.MonkeyPatch, record_spy: list[str]
) -> None:
    async def _no_redis(_cfg: Any) -> None:
        return None

    monkeypatch.setattr(redis_module, "get_redis", _no_redis)

    task = "ratatoskr.chronic.fallback"
    mw = _make_middleware()
    for _ in range(_CHRONIC_FAILURE_THRESHOLD):
        await mw.post_execute(_FakeMessage(task), _FakeResult(is_err=True))

    # Single-worker / Redis-down mode still fires via the in-process counter.
    assert record_spy == [task]
