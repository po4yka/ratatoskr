from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.infrastructure.cache import redis_cache as redis_cache_module
from app.infrastructure.cache.redis_cache import RedisCache
from app.observability import metrics


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        redis=SimpleNamespace(
            enabled=True,
            cache_enabled=True,
            required=False,
            prefix="test",
            cache_timeout_sec=0.1,
        )
    )


class _FakeRedis:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def get(self, key: str) -> str | None:
        return self._values.get(key)


class _FailingRedis:
    async def get(self, key: str) -> str | None:
        raise TimeoutError


class _WritableRedis:
    def __init__(self) -> None:
        self.set_calls: list[tuple[str, str, int]] = []
        self.scan_calls = 0

    async def set(self, key: str, value: str, *, ex: int) -> None:
        self.set_calls.append((key, value, ex))

    async def scan(self, cursor: int, *, match: str, count: int) -> tuple[int, list[str]]:
        del cursor, match, count
        self.scan_calls += 1
        return 0, ["test:llm:item"]

    async def delete(self, *keys: str) -> int:
        return len(keys)


@pytest.mark.skipif(not metrics.PROMETHEUS_AVAILABLE, reason="prometheus_client not installed")
def test_cache_metrics_bound_all_labels() -> None:
    registry = metrics.REGISTRY
    assert registry is not None

    before = (
        registry.get_sample_value(
            "ratatoskr_redis_cache_operations_total",
            {"operation": "other", "outcome": "error", "namespace": "other"},
        )
        or 0.0
    )
    metrics.record_redis_cache_operation(
        operation="arbitrary-operation",
        outcome="arbitrary-outcome",
        namespace="user-provided-cache-key",
        latency_seconds=0.01,
    )

    after = registry.get_sample_value(
        "ratatoskr_redis_cache_operations_total",
        {"operation": "other", "outcome": "error", "namespace": "other"},
    )
    assert (after or 0.0) - before == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_redis_cache_records_outcomes_without_key_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(redis_cache_module, "record_redis_cache_operation", _record)
    cache = RedisCache(_config())
    cache._client = _FakeRedis({"test:llm:private-user-id": '{"ok": true}'})

    assert await cache.get_json("llm", "private-user-id") == {"ok": True}
    assert await cache.get_json("tenant-123", "private-user-id") is None

    assert [call["outcome"] for call in calls] == ["hit", "miss"]
    assert [call["namespace"] for call in calls] == ["llm", "other"]
    assert all(call["operation"] == "get" for call in calls)
    assert all(call["latency_seconds"] >= 0 for call in calls)
    assert all("key" not in call for call in calls)


@pytest.mark.asyncio
async def test_redis_cache_records_get_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        redis_cache_module,
        "record_redis_cache_operation",
        lambda **kwargs: calls.append(kwargs),
    )
    cache = RedisCache(_config())
    cache._client = _FailingRedis()

    assert await cache.get_json("auth", "private-token-hash") is None

    assert len(calls) == 1
    assert calls[0]["operation"] == "get"
    assert calls[0]["outcome"] == "error"
    assert calls[0]["namespace"] == "auth"
    assert "key" not in calls[0]


@pytest.mark.asyncio
async def test_redis_cache_records_set_and_clear_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        redis_cache_module,
        "record_redis_cache_operation",
        lambda **kwargs: calls.append(kwargs),
    )
    cache = RedisCache(_config())
    cache._client = _WritableRedis()

    assert await cache.set_json(value={"ok": True}, ttl_seconds=60, parts=("llm", "item"))
    assert await cache.clear_prefix("llm") == 1

    assert [(call["operation"], call["outcome"]) for call in calls] == [
        ("set", "success"),
        ("clear", "success"),
    ]
    assert all(call["namespace"] == "llm" for call in calls)
    assert all("key" not in call for call in calls)
