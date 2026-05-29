"""Unit tests for app/infrastructure/cache/embedding_cache.py."""

from __future__ import annotations

import asyncio
import math
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.infrastructure.cache.embedding_cache import EmbeddingCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cache(enabled: bool = True) -> tuple[EmbeddingCache, MagicMock]:
    """Return (EmbeddingCache, redis_mock) so callers can configure mock methods."""
    redis_mock = MagicMock()
    redis_mock.enabled = enabled
    cfg = MagicMock()
    cfg.redis.embedding_cache_ttl_seconds = 86400
    return EmbeddingCache(redis_mock, cfg), redis_mock


# ---------------------------------------------------------------------------
# hash_content
# ---------------------------------------------------------------------------


class TestHashContent:
    def test_returns_32_char_hex_string(self) -> None:
        result = EmbeddingCache.hash_content("hello world")
        assert len(result) == 32
        assert all(c in "0123456789abcdef" for c in result)

    def test_same_input_produces_same_hash(self) -> None:
        assert EmbeddingCache.hash_content("abc") == EmbeddingCache.hash_content("abc")

    def test_different_inputs_produce_different_hashes(self) -> None:
        assert EmbeddingCache.hash_content("abc") != EmbeddingCache.hash_content("def")


# ---------------------------------------------------------------------------
# serialize / deserialize round-trip
# ---------------------------------------------------------------------------


class TestSerializeDeserialize:
    def test_list_of_floats_round_trips(self) -> None:
        original = [0.1, 0.2, 0.3, -0.4, 1.0]
        encoded = EmbeddingCache.serialize_embedding(original)
        recovered = EmbeddingCache.deserialize_embedding(encoded)
        assert len(recovered) == len(original)
        for a, b in zip(recovered, original, strict=True):
            assert math.isclose(a, b, rel_tol=1e-5)

    def test_numpy_like_object_with_tolist_is_accepted(self) -> None:
        class FakeNdarray:
            def tolist(self) -> list[float]:
                return [1.0, 2.0]

        encoded = EmbeddingCache.serialize_embedding(FakeNdarray())
        recovered = EmbeddingCache.deserialize_embedding(encoded)
        assert recovered == pytest.approx([1.0, 2.0])

    def test_empty_vector_round_trips(self) -> None:
        encoded = EmbeddingCache.serialize_embedding([])
        recovered = EmbeddingCache.deserialize_embedding(encoded)
        assert recovered == []

    def test_single_element_round_trips(self) -> None:
        encoded = EmbeddingCache.serialize_embedding([3.14])
        recovered = EmbeddingCache.deserialize_embedding(encoded)
        assert math.isclose(recovered[0], 3.14, rel_tol=1e-5)


# ---------------------------------------------------------------------------
# enabled property
# ---------------------------------------------------------------------------


class TestEnabled:
    def test_delegates_to_redis_cache_enabled(self) -> None:
        ec, _ = _make_cache(enabled=True)
        assert ec.enabled is True

    def test_disabled_when_redis_cache_disabled(self) -> None:
        ec, _ = _make_cache(enabled=False)
        assert ec.enabled is False


# ---------------------------------------------------------------------------
# get  (async, mocked RedisCache)
# ---------------------------------------------------------------------------


class TestGet:
    @pytest.mark.asyncio
    async def test_returns_none_when_cache_disabled(self) -> None:
        ec, _ = _make_cache(enabled=False)
        result = await ec.get("hash123", "model-v1")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_cache_miss(self) -> None:
        ec, redis_mock = _make_cache()
        redis_mock.get_json = AsyncMock(return_value=None)
        result = await ec.get("hash123", "model-v1")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_deserialized_embedding_on_hit(self) -> None:
        ec, redis_mock = _make_cache()
        original = [0.1, 0.5, -0.9]
        encoded = EmbeddingCache.serialize_embedding(original)
        redis_mock.get_json = AsyncMock(return_value={"embedding": encoded, "dimensions": 3})
        result = await ec.get("hash123", "model-v1")
        assert result is not None
        assert len(result) == 3
        for a, b in zip(result, original, strict=True):
            assert math.isclose(a, b, rel_tol=1e-5)

    @pytest.mark.asyncio
    async def test_returns_none_when_embedding_key_missing(self) -> None:
        ec, redis_mock = _make_cache()
        redis_mock.get_json = AsyncMock(return_value={"dimensions": 3})
        result = await ec.get("hash123", "model-v1")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_deserialize_error(self) -> None:
        ec, redis_mock = _make_cache()
        redis_mock.get_json = AsyncMock(return_value={"embedding": "!!!invalid_base64!!!"})
        result = await ec.get("hash123", "model-v1")
        assert result is None


# ---------------------------------------------------------------------------
# set  (async, mocked RedisCache)
# ---------------------------------------------------------------------------


class TestSet:
    @pytest.mark.asyncio
    async def test_returns_false_when_cache_disabled(self) -> None:
        ec, _ = _make_cache(enabled=False)
        result = await ec.set("hash123", "model-v1", [0.1, 0.2])
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_true_on_success(self) -> None:
        ec, redis_mock = _make_cache()
        redis_mock.set_json = AsyncMock(return_value=True)
        result = await ec.set("hash123", "model-v1", [0.1, 0.2])
        assert result is True

    @pytest.mark.asyncio
    async def test_passes_ttl_from_config(self) -> None:
        ec, redis_mock = _make_cache()
        ec._cfg.redis.embedding_cache_ttl_seconds = 3600
        redis_mock.set_json = AsyncMock(return_value=True)
        await ec.set("hash123", "model-v1", [0.1])
        call_kwargs = redis_mock.set_json.call_args
        assert call_kwargs.kwargs.get("ttl_seconds") == 3600


# ---------------------------------------------------------------------------
# get_or_compute  (async, cache-aside pattern)
# ---------------------------------------------------------------------------


class TestGetOrCompute:
    @pytest.mark.asyncio
    async def test_returns_cached_embedding_without_calling_compute(self) -> None:
        ec, redis_mock = _make_cache()
        original = [1.0, 2.0, 3.0]
        encoded = EmbeddingCache.serialize_embedding(original)
        redis_mock.get_json = AsyncMock(return_value={"embedding": encoded, "dimensions": 3})

        compute_fn = AsyncMock(return_value=[9.0, 9.0, 9.0])
        result = await ec.get_or_compute("some text", "model-v1", compute_fn)

        compute_fn.assert_not_called()
        for a, b in zip(result, original, strict=True):
            assert math.isclose(a, b, rel_tol=1e-5)

    @pytest.mark.asyncio
    async def test_calls_compute_on_cache_miss_and_caches_result(self) -> None:
        ec, redis_mock = _make_cache()
        redis_mock.get_json = AsyncMock(return_value=None)
        redis_mock.set_json = AsyncMock(return_value=True)

        computed = [0.1, 0.2, 0.3]
        compute_fn = AsyncMock(return_value=computed)

        result = await ec.get_or_compute("some text", "model-v1", compute_fn)

        compute_fn.assert_called_once_with("some text")
        assert result == pytest.approx(computed)
        redis_mock.set_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_list_when_compute_returns_numpy_like(self) -> None:
        ec, redis_mock = _make_cache()
        redis_mock.get_json = AsyncMock(return_value=None)
        redis_mock.set_json = AsyncMock(return_value=True)

        class FakeNdarray:
            def tolist(self) -> list[float]:
                return [7.0, 8.0]

        compute_fn = AsyncMock(return_value=FakeNdarray())
        result = await ec.get_or_compute("text", "model", compute_fn)
        assert result == [7.0, 8.0]


# ---------------------------------------------------------------------------
# Singleflight / stampede protection
# ---------------------------------------------------------------------------


class TestSingleflight:
    """Concurrent misses for the same key must call compute_fn exactly once."""

    @pytest.mark.asyncio
    async def test_concurrent_misses_call_compute_fn_once(self) -> None:
        """N concurrent get_or_compute calls for the same text trigger one compute."""
        ec, redis_mock = _make_cache()

        compute_call_count = 0
        computed_value = [0.1, 0.2, 0.3]

        # Simulate a slow compute (yields control so all callers pile up)
        async def slow_compute(text: str) -> list[float]:
            nonlocal compute_call_count
            compute_call_count += 1
            await asyncio.sleep(0)  # yield to let other coroutines run
            return computed_value

        # Cache always misses on get_json; set_json succeeds.
        # After the first set, subsequent get_json calls return the stored value
        # so the double-checked read inside the lock succeeds for waiters.
        stored: dict[str, object] = {}

        async def fake_get_json(*parts: str) -> object:
            key = ":".join(parts)
            return stored.get(key)

        async def fake_set_json(*, value: object, ttl_seconds: int, parts: tuple[str, ...]) -> bool:
            key = ":".join(parts)
            stored[key] = value
            return True

        redis_mock.get_json = fake_get_json
        redis_mock.set_json = fake_set_json

        # Fire 10 concurrent requests for identical text.
        results = await asyncio.gather(
            *[ec.get_or_compute("same text", "model-v1", slow_compute) for _ in range(10)]
        )

        assert compute_call_count == 1, (
            f"compute_fn called {compute_call_count} times; expected 1 (singleflight broken)"
        )
        for r in results:
            assert r == pytest.approx(computed_value)

    @pytest.mark.asyncio
    async def test_different_keys_compute_independently(self) -> None:
        """Different texts each get their own compute, not collapsed together."""
        ec, redis_mock = _make_cache()

        compute_call_count = 0

        async def count_compute(text: str) -> list[float]:
            nonlocal compute_call_count
            compute_call_count += 1
            await asyncio.sleep(0)
            return [float(ord(text[0]))]

        redis_mock.get_json = AsyncMock(return_value=None)
        redis_mock.set_json = AsyncMock(return_value=True)

        texts = ["alpha", "beta", "gamma"]
        await asyncio.gather(*[ec.get_or_compute(t, "model-v1", count_compute) for t in texts])

        assert compute_call_count == len(texts)

    @pytest.mark.asyncio
    async def test_singleflight_works_when_redis_disabled(self) -> None:
        """Even with Redis off, concurrent calls for the same text call compute once."""
        ec, _ = _make_cache(enabled=False)

        compute_call_count = 0

        async def slow_compute(text: str) -> list[float]:
            nonlocal compute_call_count
            compute_call_count += 1
            await asyncio.sleep(0)
            return [1.0, 2.0]

        results = await asyncio.gather(
            *[ec.get_or_compute("text", "model-v1", slow_compute) for _ in range(5)]
        )

        assert compute_call_count == 1, (
            f"compute_fn called {compute_call_count} times with Redis disabled; expected 1"
        )
        for r in results:
            assert r == pytest.approx([1.0, 2.0])

    @pytest.mark.asyncio
    async def test_lock_dict_cleaned_up_after_completion(self) -> None:
        """The internal lock registry is empty once all waiters have completed."""
        ec, redis_mock = _make_cache()
        redis_mock.get_json = AsyncMock(return_value=None)
        redis_mock.set_json = AsyncMock(return_value=True)

        async def compute(text: str) -> list[float]:
            await asyncio.sleep(0)
            return [0.5]

        await asyncio.gather(
            *[ec.get_or_compute("cleanup text", "model-v1", compute) for _ in range(4)]
        )

        assert ec._inflight._locks == {}, "Lock registry should be empty after all waiters finish"
        assert ec._inflight._waiters == {}
