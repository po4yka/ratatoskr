"""Branch coverage for ``SummaryCacheAdapter.get`` / ``.set`` + key parity lock.

The scope-collision shape is pinned in ``test_summary_cache_adapter_scope.py``.
This module exercises the disabled / empty / non-dict / missing-fields / hit
branches of ``get`` and the no-op / store branches of ``set``, then pins the
adapter key tuple against ``LLMSummaryCache``'s parts so a future divergence in
the shared ``("llm", ..., prompt_version, lang, url_hash)`` shape fails CI.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.adapters.content.llm_summarizer_cache import LLMSummaryCache
from app.adapters.content.summary_cache_adapter import SummaryCacheAdapter

_SUMMARY = {"tldr": "t", "summary_250": "s", "summary_1000": "l"}


class _FakeCache:
    """Records get/set key ``parts``; ``enabled`` is constructor-controlled."""

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self.store: dict[tuple[str, ...], Any] = {}
        self.get_parts: list[tuple[str, ...]] = []
        self.set_calls: list[tuple[tuple[str, ...], int, Any]] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def get_json(self, *parts: str) -> Any | None:
        self.get_parts.append(parts)
        return self.store.get(parts)

    async def set_json(self, *, value: Any, ttl_seconds: int, parts: Any) -> bool:
        key = tuple(parts)
        self.set_calls.append((key, ttl_seconds, value))
        self.store[key] = value
        return True

    async def clear(self) -> int:
        self.store.clear()
        return 0


# ---------------------------------------------------------------------------
# get()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_none_when_cache_disabled() -> None:
    cache = _FakeCache(enabled=False)
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3")

    assert await adapter.get("urlhash", "en") is None
    # Disabled short-circuits before any backend read.
    assert cache.get_parts == []


@pytest.mark.asyncio
async def test_get_returns_none_when_url_hash_empty() -> None:
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3")

    assert await adapter.get("", "en") is None
    assert cache.get_parts == []


@pytest.mark.asyncio
async def test_get_returns_none_when_cached_value_not_dict() -> None:
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3")
    cache.store[("llm", "dev", "public", "v3", "en", "urlhash")] = "not-a-dict"

    assert await adapter.get("urlhash", "en") is None


@pytest.mark.asyncio
async def test_get_returns_none_when_cached_value_missing_required_fields() -> None:
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3")
    # Present dict, but the contract fields are empty -> treated as a miss.
    cache.store[("llm", "dev", "public", "v3", "en", "urlhash")] = {
        "tldr": "",
        "summary_250": "",
        "summary_1000": "",
    }

    assert await adapter.get("urlhash", "en") is None


@pytest.mark.asyncio
async def test_get_returns_payload_on_hit() -> None:
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3")
    cache.store[("llm", "dev", "public", "v3", "en", "urlhash")] = _SUMMARY

    assert await adapter.get("urlhash", "en") == _SUMMARY


@pytest.mark.asyncio
async def test_get_defaults_lang_to_auto() -> None:
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3")

    await adapter.get("urlhash", "")

    assert cache.get_parts == [("llm", "dev", "public", "v3", "auto", "urlhash")]


# ---------------------------------------------------------------------------
# set()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_is_noop_when_cache_disabled() -> None:
    cache = _FakeCache(enabled=False)
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3")

    await adapter.set("urlhash", "en", _SUMMARY)

    assert cache.set_calls == []


@pytest.mark.asyncio
async def test_set_is_noop_when_url_hash_empty() -> None:
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3")

    await adapter.set("", "en", _SUMMARY)

    assert cache.set_calls == []


@pytest.mark.asyncio
async def test_set_is_noop_when_summary_empty() -> None:
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3")

    await adapter.set("urlhash", "en", {})

    assert cache.set_calls == []


@pytest.mark.asyncio
async def test_set_is_noop_when_summary_not_dict() -> None:
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3")

    await adapter.set("urlhash", "en", "not-a-dict")  # type: ignore[arg-type]

    assert cache.set_calls == []


@pytest.mark.asyncio
async def test_set_stores_with_configured_ttl() -> None:
    cache = _FakeCache()
    adapter = SummaryCacheAdapter(cache=cache, prompt_version="v3", ttl_seconds=1234)

    await adapter.set("urlhash", "en", _SUMMARY)

    assert len(cache.set_calls) == 1
    parts, ttl, value = cache.set_calls[0]
    assert parts == ("llm", "dev", "public", "v3", "en", "urlhash")
    assert ttl == 1234
    assert value == _SUMMARY


# ---------------------------------------------------------------------------
# Parity lock against LLMSummaryCache's key scheme
# ---------------------------------------------------------------------------


def _legacy_summary_parts(prompt_version: str, lang_key: str, url_hash: str) -> tuple[str, ...]:
    """The parts tuple ``LLMSummaryCache.write_summary_cache`` passes to the cache.

    Mirrored here from the source so this test fails (not silently agrees) if the
    legacy scheme is edited. Cross-checked against the live source string below.
    """
    return ("llm", prompt_version, lang_key or "auto", url_hash)


def test_legacy_summary_parts_helper_matches_live_source() -> None:
    """Guard the mirrored tuple above against drift in the real method body."""
    src = inspect.getsource(LLMSummaryCache.write_summary_cache)
    assert 'parts=("llm", self._prompt_version, chosen_lang or "auto", url_hash)' in src


def test_adapter_key_is_legacy_key_with_scope_prefix_injected() -> None:
    """The graph adapter key == the legacy key + (environment, user_scope) scope.

    Byte-parity is *intentionally dropped* (the adapter namespaces by
    environment/user_scope), but the shared tail must stay aligned: stripping the
    two scope segments from the adapter key yields exactly the legacy tuple. If
    either scheme reorders the prompt_version/lang/url_hash tail, this assertion
    fails and surfaces the drift in CI.
    """
    adapter = SummaryCacheAdapter(
        cache=_FakeCache(),
        prompt_version="v3",
        environment="prod",
        user_scope="tenant-7",
    )

    adapter_key = adapter._key_parts("en", "urlhash")
    legacy_key = _legacy_summary_parts("v3", "en", "urlhash")

    assert adapter_key[0] == legacy_key[0] == "llm"
    # Segments 1 and 2 are the scope prefix unique to the graph adapter.
    assert adapter_key[1:3] == ("prod", "tenant-7")
    # The remaining tail must equal the legacy key tail byte-for-byte.
    assert adapter_key[3:] == legacy_key[1:] == ("v3", "en", "urlhash")
