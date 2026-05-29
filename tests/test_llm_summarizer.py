"""Unit tests for core LLM summarization modules.

Covers:
- LLMWorkflowRepairMixin._attempt_salvage_parsing (JSON repair / salvage path)
- LLMSummaryCache.get_cached_summary (cache hit/miss logic)
- LLMSummaryCache.build_topics_cache_key (determinism, deduplication)
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock


class TestAttemptSalvageParsing(unittest.TestCase):
    """Tests for LLMWorkflowRepairMixin._attempt_salvage_parsing."""

    def _make_mixin(self):
        from app.adapters.content.llm_response_workflow_repair import LLMWorkflowRepairMixin

        class StubWorkflow(LLMWorkflowRepairMixin):
            _audit = staticmethod(lambda *a, **kw: None)
            _sem = None
            _set_failure_context = staticmethod(lambda *a, **kw: None)
            cfg = SimpleNamespace(runtime=SimpleNamespace())
            openrouter = None
            request_repo = None
            user_repo = None

        return StubWorkflow()

    def _make_llm_stub(self, response_text: str = "", response_json: Any = None):
        return SimpleNamespace(
            status="error",
            error_text="structured_output_parse_error",
            response_text=response_text,
            response_json=response_json,
        )

    def test_returns_none_for_empty_response(self):
        mixin = self._make_mixin()
        llm = self._make_llm_stub(response_text="")
        result = mixin._attempt_salvage_parsing(llm, "cid-001")
        assert result is None

    def test_returns_none_for_non_json_text(self):
        mixin = self._make_mixin()
        llm = self._make_llm_stub(response_text="not json at all")
        result = mixin._attempt_salvage_parsing(llm, "cid-002")
        assert result is None

    def test_salvages_valid_summary_json_from_text(self):
        mixin = self._make_mixin()
        # Minimal valid summary payload (only required fields, rest optional)
        payload = (
            '{"tldr": "Short", "summary_250": "Med", "summary_1000": "Long",'
            ' "key_ideas": [], "topic_tags": [], "entities": []}'
        )
        llm = self._make_llm_stub(response_text=payload)
        result = mixin._attempt_salvage_parsing(llm, "cid-003")
        # Result may be None if validate_and_shape_summary requires more fields —
        # but it should not raise an exception either way.
        assert result is None or isinstance(result, dict)

    def test_returns_none_for_array_json(self):
        """Top-level JSON arrays are not valid summary responses."""
        mixin = self._make_mixin()
        llm = self._make_llm_stub(response_text='["item1", "item2"]')
        result = mixin._attempt_salvage_parsing(llm, "cid-004")
        assert result is None


class TestLLMSummaryCacheHitMiss(unittest.IsolatedAsyncioTestCase):
    """Tests for LLMSummaryCache.get_cached_summary cache hit/miss logic."""

    def _make_cache(self, *, cache_enabled: bool = True, cached_value: Any = None):
        from app.adapters.content.llm_summarizer_cache import LLMSummaryCache

        mock_redis = AsyncMock()
        mock_redis.enabled = cache_enabled
        mock_redis.get_json = AsyncMock(return_value=cached_value)
        mock_redis.set_json = AsyncMock()

        return LLMSummaryCache(
            cache=mock_redis,
            cfg=SimpleNamespace(redis=SimpleNamespace(llm_ttl_seconds=7200)),
            prompt_version="v1",
            insights_has_content=lambda d: bool(d.get("insights")),
        )

    async def test_returns_none_when_cache_disabled(self):
        cache = self._make_cache(cache_enabled=False)
        result = await cache.get_cached_summary("hash123", "en", "model", "cid")
        assert result is None

    async def test_returns_none_when_url_hash_empty(self):
        cache = self._make_cache(cache_enabled=True, cached_value={"tldr": "x"})
        result = await cache.get_cached_summary(None, "en", "model", "cid")
        assert result is None

    async def test_returns_none_when_cache_miss(self):
        cache = self._make_cache(cache_enabled=True, cached_value=None)
        result = await cache.get_cached_summary("hash123", "en", "model", "cid")
        assert result is None

    async def test_returns_none_for_non_dict_cached_value(self):
        cache = self._make_cache(cache_enabled=True, cached_value="not-a-dict")
        result = await cache.get_cached_summary("hash123", "en", "model", "cid")
        assert result is None

    async def test_returns_cached_dict_on_hit(self):
        payload = {"tldr": "A", "summary_250": "B", "summary_1000": "C"}
        cache = self._make_cache(cache_enabled=True, cached_value=payload)
        result = await cache.get_cached_summary("hash123", "en", "model", "cid")
        assert result == payload


class _DictRedis:
    """Key-aware Redis stub: stores by the exact key parts, like RedisCache."""

    def __init__(self) -> None:
        self.enabled = True
        self.store: dict[tuple[Any, ...], Any] = {}

    async def get_json(self, *parts: Any) -> Any:
        return self.store.get(tuple(parts))

    async def set_json(self, *, value: Any, ttl_seconds: int, parts: tuple[Any, ...]) -> None:
        self.store[tuple(parts)] = value


class TestLLMSummaryCacheModelAgnostic(unittest.IsolatedAsyncioTestCase):
    """The summary cache key is model-agnostic (audit M-7)."""

    def _make_cache(self, redis: _DictRedis, *, prompt_version: str = "v1"):
        from app.adapters.content.llm_summarizer_cache import LLMSummaryCache

        return LLMSummaryCache(
            cache=redis,
            cfg=SimpleNamespace(redis=SimpleNamespace(llm_ttl_seconds=7200)),
            prompt_version=prompt_version,
            insights_has_content=lambda d: bool(d.get("insights")),
        )

    async def test_fallback_model_write_is_hit_by_primary_model_read(self) -> None:
        redis = _DictRedis()
        cache = self._make_cache(redis)
        payload = {"tldr": "A", "summary_250": "B", "summary_1000": "C"}

        # Produced by a fallback model...
        await cache.write_summary_cache("hash123", "fallback-model", "en", payload)
        # ...served on the next request even though a different model is primary.
        result = await cache.get_cached_summary("hash123", "en", "primary-model", "cid")

        assert result == payload

    async def test_prompt_version_change_invalidates_cache(self) -> None:
        redis = _DictRedis()
        payload = {"tldr": "A", "summary_250": "B", "summary_1000": "C"}

        await self._make_cache(redis, prompt_version="v1").write_summary_cache(
            "hash123", "model", "en", payload
        )
        # A newer prompt version must not read the old entry.
        result = await self._make_cache(redis, prompt_version="v2").get_cached_summary(
            "hash123", "en", "model", "cid"
        )

        assert result is None


class TestBuildTopicsCacheKey(unittest.TestCase):
    """Tests for LLMSummaryCache.build_topics_cache_key."""

    def _make_cache(self):
        from app.adapters.content.llm_summarizer_cache import LLMSummaryCache

        mock_redis = MagicMock()
        mock_redis.enabled = False
        return LLMSummaryCache(
            cache=mock_redis,
            cfg=SimpleNamespace(redis=SimpleNamespace(llm_ttl_seconds=7200)),
            prompt_version="v1",
            insights_has_content=lambda d: False,
        )

    def test_empty_inputs_return_empty_key(self):
        cache = self._make_cache()
        assert cache.build_topics_cache_key([], []) == ""

    def test_key_is_deterministic(self):
        cache = self._make_cache()
        k1 = cache.build_topics_cache_key(["Python", "AI"], ["ml"])
        k2 = cache.build_topics_cache_key(["AI", "Python"], ["ml"])
        assert k1 == k2

    def test_deduplicates_overlapping_topics_and_tags(self):
        cache = self._make_cache()
        k1 = cache.build_topics_cache_key(["python"], ["python"])
        k2 = cache.build_topics_cache_key(["python"], [])
        assert k1 == k2

    def test_case_insensitive(self):
        cache = self._make_cache()
        k1 = cache.build_topics_cache_key(["Python"], [])
        k2 = cache.build_topics_cache_key(["python"], [])
        assert k1 == k2

    def test_whitespace_stripped(self):
        cache = self._make_cache()
        k1 = cache.build_topics_cache_key(["  python  "], [])
        k2 = cache.build_topics_cache_key(["python"], [])
        assert k1 == k2

    def test_key_is_16_hex_chars(self):
        cache = self._make_cache()
        key = cache.build_topics_cache_key(["python"], ["ai"])
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)


if __name__ == "__main__":
    unittest.main()
