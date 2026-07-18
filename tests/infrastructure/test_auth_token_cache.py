from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.config import AppConfig
from app.infrastructure.cache.auth_token_cache import AuthTokenCache
from app.infrastructure.cache.redis_cache import RedisCache


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


def _cache(**kwargs: Any) -> _FakeRedisCache:
    return _FakeRedisCache(**kwargs)


def _redis(cache: _FakeRedisCache) -> RedisCache:
    return cast("RedisCache", cache)


def _cfg() -> AppConfig:
    return cast(
        "AppConfig",
        SimpleNamespace(
            redis=SimpleNamespace(prefix="ratatoskr:test", auth_token_cache_ttl_seconds=123)
        ),
    )


@pytest.mark.asyncio
async def test_auth_token_cache_noops_when_disabled() -> None:
    cache = _cache(enabled=False)
    service = AuthTokenCache(_redis(cache), _cfg())

    assert service.enabled is False
    assert await service.get_token("abc") is None
    assert (
        await service.set_token(
            "abc",
            user_id=1,
            client_id=None,
            expires_at="soon",
            remember_me=True,
            family_id="family-abc",
        )
        is False
    )
    assert await service.invalidate_token("abc") is False


@pytest.mark.asyncio
async def test_auth_token_cache_stores_rotation_policy_fields() -> None:
    cache = _cache()
    service = AuthTokenCache(_redis(cache), _cfg())

    ok = await service.set_token(
        "abc123456",
        user_id=42,
        client_id="mobile",
        expires_at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        is_revoked=False,
        token_id=99,
        remember_me=False,
        family_id="family-123",
        parent_token_hash="parent-456",
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
                "remember_me": False,
                "family_id": "family-123",
                "parent_token_hash": "parent-456",
            },
            "ttl_seconds": 123,
            "parts": ("auth", "token", "abc123456"),
        }
    ]


@pytest.mark.asyncio
async def test_auth_token_cache_treats_legacy_rotation_metadata_as_a_miss() -> None:
    cache = _cache(
        cached={
            "user_id": 42,
            "client_id": "mobile",
            "expires_at": "2026-01-02T03:04:00+00:00",
            "is_revoked": False,
        }
    )
    service = AuthTokenCache(_redis(cache), _cfg())

    assert await service.get_token("legacy-token") is None


@pytest.mark.asyncio
async def test_auth_token_cache_get_and_mark_revoked_round_trip() -> None:
    cached = {
        "user_id": 7,
        "is_revoked": False,
        "remember_me": True,
        "family_id": "family-7",
        "parent_token_hash": None,
    }
    cache = _cache(cached=cached)
    service = AuthTokenCache(_redis(cache), _cfg())

    assert await service.get_token("hash-value") == cached
    assert await service.mark_revoked("hash-value") is True

    assert cache.set_calls[-1]["value"]["is_revoked"] is True
    assert cache.set_calls[-1]["parts"] == ("auth", "token", "hash-value")


@pytest.mark.asyncio
async def test_auth_token_cache_invalidate_uses_configured_redis_key() -> None:
    client = _FakeRedisClient()
    cache = _cache(client=client)
    service = AuthTokenCache(_redis(cache), _cfg())

    assert await service.invalidate_token("token-hash") is True
    assert client.deleted == ["ratatoskr:test:auth:token:token-hash"]


@pytest.mark.asyncio
async def test_auth_token_cache_invalidate_reports_redis_failure() -> None:
    cache = _cache(client=_FakeRedisClient(raises=True))
    service = AuthTokenCache(_redis(cache), _cfg())

    assert await service.invalidate_token("token-hash") is False


@pytest.mark.asyncio
async def test_mark_revoked_writes_tombstone_for_never_cached_token() -> None:
    """A token revoked while NOT previously cached must write a revocation
    tombstone so that a subsequent get_token call returns is_revoked=True
    instead of None (which would fall through to DB and risk a stale hit).
    """
    from app.infrastructure.cache.auth_token_cache import _REVOCATION_TOMBSTONE_TTL_SECONDS

    # Cache starts empty (no prior set_token call for this hash)
    cache = _cache(cached=None)
    service = AuthTokenCache(_redis(cache), _cfg())

    result = await service.mark_revoked("never-seen-hash")

    assert result is True, "mark_revoked must succeed even when token was never cached"
    assert len(cache.set_calls) == 1
    tombstone_call = cache.set_calls[0]
    assert tombstone_call["value"]["is_revoked"] is True
    assert tombstone_call["parts"] == ("auth", "token", "never-seen-hash")
    # Tombstone must use the short dedicated TTL, not the full auth-token TTL
    assert tombstone_call["ttl_seconds"] == _REVOCATION_TOMBSTONE_TTL_SECONDS
    # Tombstone TTL must be shorter than the production default (604800 s / 7 days)
    assert tombstone_call["ttl_seconds"] < 604_800, (
        "tombstone TTL must be shorter than the production auth token TTL to limit Redis pollution"
    )


@pytest.mark.asyncio
async def test_mark_revoked_never_cached_token_cannot_be_served_as_valid() -> None:
    """End-to-end: revoke a never-cached token, then simulate get_token reading
    back the tombstone — must return is_revoked=True, not None or False.
    """
    # Phase 1: token not in cache, revocation fires
    cache = _cache(cached=None)
    service = AuthTokenCache(_redis(cache), _cfg())
    await service.mark_revoked("target-hash")

    # Phase 2: simulate the tombstone now sitting in Redis (what set_json stored)
    assert len(cache.set_calls) == 1
    tombstone_value = cache.set_calls[0]["value"]
    assert tombstone_value["is_revoked"] is True

    # Phase 3: next get_token call reads back the tombstone — must NOT appear valid
    cache2 = _cache(cached=tombstone_value)
    service2 = AuthTokenCache(_redis(cache2), _cfg())
    token_data = await service2.get_token("target-hash")

    assert token_data is not None, "tombstone should be returned (not a cache miss)"
    assert token_data["is_revoked"] is True, (
        "token revoked while not in cache must be served as revoked, never as valid"
    )


@pytest.mark.asyncio
async def test_mark_revoked_noops_when_cache_disabled() -> None:
    cache = _cache(enabled=False)
    service = AuthTokenCache(_redis(cache), _cfg())

    result = await service.mark_revoked("any-hash")

    assert result is False
    assert cache.set_calls == []
