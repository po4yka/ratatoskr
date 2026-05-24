from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.infrastructure.cache.auth_token_cache import AuthTokenCache


class _FakeRedisCache:
    def __init__(self, *, enabled: bool = True, cached: Any = None, client: Any = None) -> None:
        self.enabled = enabled
        self.cached = cached
        self.client = client
        self.set_calls: list[dict[str, Any]] = []

    async def get_json(self, *parts: str) -> Any:
        self.last_get_parts = parts
        return self.cached

    async def set_json(
        self,
        *,
        value: dict[str, Any],
        ttl_seconds: int,
        parts: tuple[str, ...],
    ) -> bool:
        self.set_calls.append({"value": value, "ttl_seconds": ttl_seconds, "parts": parts})
        return True

    async def _get_client(self) -> Any:
        return self.client


class _FakeRedisClient:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.deleted: list[str] = []

    async def delete(self, key: str) -> None:
        if self.raises:
            raise RuntimeError("redis down")
        self.deleted.append(key)


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        redis=SimpleNamespace(prefix="ratatoskr:test", auth_token_cache_ttl_seconds=123)
    )


@pytest.mark.asyncio
async def test_auth_token_cache_noops_when_disabled() -> None:
    cache = _FakeRedisCache(enabled=False)
    service = AuthTokenCache(cache, _cfg())

    assert service.enabled is False
    assert await service.get_token("abc") is None
    assert await service.set_token("abc", user_id=1, client_id=None, expires_at="soon") is False
    assert await service.invalidate_token("abc") is False


@pytest.mark.asyncio
async def test_auth_token_cache_stores_datetime_and_token_id() -> None:
    cache = _FakeRedisCache()
    service = AuthTokenCache(cache, _cfg())

    ok = await service.set_token(
        "abc123456",
        user_id=42,
        client_id="mobile",
        expires_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        is_revoked=False,
        token_id=99,
    )

    assert ok is True
    assert cache.set_calls == [
        {
            "value": {
                "user_id": 42,
                "client_id": "mobile",
                "expires_at": "2026-01-02T03:04:00+00:00",
                "is_revoked": False,
                "id": 99,
            },
            "ttl_seconds": 123,
            "parts": ("auth", "token", "abc123456"),
        }
    ]


@pytest.mark.asyncio
async def test_auth_token_cache_get_and_mark_revoked_round_trip() -> None:
    cache = _FakeRedisCache(cached={"user_id": 7, "is_revoked": False})
    service = AuthTokenCache(cache, _cfg())

    assert await service.get_token("hash-value") == {"user_id": 7, "is_revoked": False}
    assert await service.mark_revoked("hash-value") is True

    assert cache.set_calls[-1]["value"]["is_revoked"] is True
    assert cache.set_calls[-1]["parts"] == ("auth", "token", "hash-value")


@pytest.mark.asyncio
async def test_auth_token_cache_invalidate_uses_configured_redis_key() -> None:
    client = _FakeRedisClient()
    cache = _FakeRedisCache(client=client)
    service = AuthTokenCache(cache, _cfg())

    assert await service.invalidate_token("token-hash") is True
    assert client.deleted == ["ratatoskr:test:auth:token:token-hash"]


@pytest.mark.asyncio
async def test_auth_token_cache_invalidate_reports_redis_failure() -> None:
    cache = _FakeRedisCache(client=_FakeRedisClient(raises=True))
    service = AuthTokenCache(cache, _cfg())

    assert await service.invalidate_token("token-hash") is False
