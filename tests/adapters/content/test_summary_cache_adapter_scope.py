"""Regression: the graph summary cache key is scoped by environment/user_scope.

A dev and a prod (or two tenant scopes) that share one Redis must never read
each other's cached summaries. The key therefore namespaces by
``environment`` and ``user_scope`` ahead of the content/lang/prompt segments.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.content.summary_cache_adapter import SummaryCacheAdapter


class _FakeCache:
    """Records the key ``parts`` of get/set calls; always enabled."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, ...], Any] = {}
        self.get_parts: list[tuple[str, ...]] = []
        self.set_parts: list[tuple[str, ...]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def get_json(self, *parts: str) -> Any | None:
        self.get_parts.append(parts)
        return self.store.get(parts)

    async def set_json(self, *, value: Any, ttl_seconds: int, parts: Any) -> bool:
        self.set_parts.append(tuple(parts))
        self.store[tuple(parts)] = value
        return True

    async def clear(self) -> int:
        self.store.clear()
        return 0


_SUMMARY = {"tldr": "t", "summary_250": "s", "summary_1000": "l"}


@pytest.mark.asyncio
async def test_set_key_includes_environment_and_user_scope() -> None:
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(
        cache=cache,
        prompt_version="v3",
        environment="prod",
        user_scope="tenant-7",
    )

    await adapter.set("urlhash", "en", _SUMMARY)

    assert cache.set_parts == [("llm", "prod", "tenant-7", "v3", "en", "urlhash")]


@pytest.mark.asyncio
async def test_get_key_includes_environment_and_user_scope() -> None:
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(
        cache=cache,
        prompt_version="v3",
        environment="prod",
        user_scope="tenant-7",
    )

    await adapter.get("urlhash", "en")

    assert cache.get_parts == [("llm", "prod", "tenant-7", "v3", "en", "urlhash")]


@pytest.mark.asyncio
async def test_different_environments_do_not_collide() -> None:
    """A prod write must not be visible to a dev reader sharing one cache."""
    cache = _FakeCache()
    prod = SummaryCacheAdapter(
        cache=cache, prompt_version="v3", environment="prod", user_scope="public"
    )
    dev = SummaryCacheAdapter(
        cache=cache, prompt_version="v3", environment="dev", user_scope="public"
    )

    await prod.set("urlhash", "en", _SUMMARY)

    assert await prod.get("urlhash", "en") == _SUMMARY
    assert await dev.get("urlhash", "en") is None


@pytest.mark.asyncio
async def test_different_user_scopes_do_not_collide() -> None:
    cache = _FakeCache()
    tenant_a = SummaryCacheAdapter(
        cache=cache, prompt_version="v3", environment="prod", user_scope="a"
    )
    tenant_b = SummaryCacheAdapter(
        cache=cache, prompt_version="v3", environment="prod", user_scope="b"
    )

    await tenant_a.set("urlhash", "en", _SUMMARY)

    assert await tenant_a.get("urlhash", "en") == _SUMMARY
    assert await tenant_b.get("urlhash", "en") is None


@pytest.mark.asyncio
async def test_empty_scope_values_fall_back_to_stable_sentinels() -> None:
    """Empty environment/user_scope collapse to fixed sentinels (stable key length)."""
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3", environment="", user_scope="")

    await adapter.set("urlhash", "en", _SUMMARY)

    assert cache.set_parts == [("llm", "dev", "public", "v3", "en", "urlhash")]
