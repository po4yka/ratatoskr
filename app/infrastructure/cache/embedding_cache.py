"""Redis cache for embedding vectors.

Caches computed embeddings by content hash to avoid recomputation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
from typing import TYPE_CHECKING, Any, cast

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.infrastructure.cache.redis_cache import RedisCache

logger = get_logger(__name__)


class _KeyedLock:
    """Per-key asyncio.Lock registry with automatic cleanup.

    Tracks waiter counts so entries are removed from the dict as soon as
    no coroutine is waiting on or holding a given key, preventing unbounded
    dict growth.

    Also maintains an in-memory result store (_results) so that the winning
    coroutine can leave its computed value for waiters to read even when Redis
    is disabled (in which case the normal double-checked Redis read returns
    None for all waiters).  The result is cleared when the last waiter exits.
    """

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._waiters: dict[str, int] = {}
        self._results: dict[str, list[float]] = {}

    def acquire(self, key: str) -> _KeyedLockContext:
        """Return an async context manager that holds the per-key lock."""
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
            self._waiters[key] = 0
        self._waiters[key] += 1
        return _KeyedLockContext(self, key)

    def _release(self, key: str) -> None:
        self._locks[key].release()
        self._waiters[key] -= 1
        if self._waiters[key] == 0:
            del self._locks[key]
            del self._waiters[key]
            self._results.pop(key, None)


class _KeyedLockContext:
    """Async context manager returned by _KeyedLock.acquire."""

    def __init__(self, registry: _KeyedLock, key: str) -> None:
        self._registry = registry
        self._key = key

    async def __aenter__(self) -> None:
        await self._registry._locks[self._key].acquire()

    async def __aexit__(self, *_: object) -> None:
        self._registry._release(self._key)


class EmbeddingCache:
    """Cache computed embedding vectors in Redis.

    Key pattern: ratatoskr:embed:v1:{model_name}:{content_hash}
    Value: {"embedding": base64_encoded_float32_array, "dimensions": int}
    TTL: 24 hours (configurable via REDIS_EMBEDDING_CACHE_TTL_SECONDS)

    Why cache embeddings?
    - Embedding generation is CPU-intensive (especially on ARM/Pi)
    - Same content produces identical embeddings
    - Many articles get re-processed (edits, re-summarization)

    Fallback: On cache miss, compute embedding (existing behavior).
    """

    def __init__(self, cache: RedisCache, cfg: AppConfig) -> None:
        self._cache = cache
        self._cfg = cfg
        self._inflight: _KeyedLock = _KeyedLock()

    @property
    def enabled(self) -> bool:
        return self._cache.enabled

    @staticmethod
    def hash_content(text: str) -> str:
        """Create a deterministic hash for content.

        Uses SHA256 truncated to 32 chars for reasonable key length.
        """
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def serialize_embedding(embedding: Any) -> str:
        """Serialize embedding vector to base64 string.

        Args:
            embedding: Numpy array or list of floats.

        Returns:
            Base64-encoded string of packed float32 values.
        """
        values: list[float] = (
            embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
        )
        packed = struct.pack(f"<{len(values)}f", *values)
        return base64.b64encode(packed).decode("ascii")

    @staticmethod
    def deserialize_embedding(encoded: str) -> list[float]:
        """Deserialize embedding from base64 string.

        Args:
            encoded: Base64-encoded string.

        Returns:
            List of float values.
        """
        packed = base64.b64decode(encoded)
        count = len(packed) // 4  # 4 bytes per float32
        return list(struct.unpack(f"<{count}f", packed))

    async def get(
        self,
        content_hash: str,
        model_name: str,
    ) -> list[float] | None:
        """Get cached embedding by content hash.

        Args:
            content_hash: SHA256 hash of the content.
            model_name: Embedding model name (for cache partitioning).

        Returns:
            List of float values or None if not cached.
        """
        if not self._cache.enabled:
            return None

        cached = await self._cache.get_json("embed", "v1", model_name, content_hash)
        if not isinstance(cached, dict):
            return None

        embedding_b64 = cached.get("embedding")
        if not isinstance(embedding_b64, str):
            return None

        try:
            embedding = self.deserialize_embedding(embedding_b64)
            logger.debug(
                "embedding_cache_hit",
                extra={
                    "model": model_name,
                    "hash": content_hash[:8],
                    "dimensions": len(embedding),
                },
            )
            return embedding
        except Exception as exc:
            logger.warning(
                "embedding_cache_deserialize_failed",
                extra={"hash": content_hash[:8], "error": str(exc)},
            )
            return None

    async def set(
        self,
        content_hash: str,
        model_name: str,
        embedding: Any,
    ) -> bool:
        """Cache an embedding vector.

        Args:
            content_hash: SHA256 hash of the content.
            model_name: Embedding model name.
            embedding: Numpy array or list of floats.

        Returns:
            True if cached successfully, False otherwise.
        """
        if not self._cache.enabled:
            return False

        try:
            # Compute the float list once; reuse it for both the serialized
            # blob and the dimensions field so we never call .tolist() twice.
            values: list[float] = (
                embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
            )
            embedding_b64 = self.serialize_embedding(values)
        except Exception as exc:
            logger.warning(
                "embedding_cache_serialize_failed",
                extra={"hash": content_hash[:8], "error": str(exc)},
            )
            return False

        value = {
            "embedding": embedding_b64,
            "dimensions": len(values),
            "model": model_name,
        }

        ttl = self._cfg.redis.embedding_cache_ttl_seconds
        success = await self._cache.set_json(
            value=value,
            ttl_seconds=ttl,
            parts=("embed", "v1", model_name, content_hash),
        )

        if success:
            logger.debug(
                "embedding_cached",
                extra={
                    "model": model_name,
                    "hash": content_hash[:8],
                    "dimensions": len(values),
                    "ttl": ttl,
                },
            )
        return success

    async def get_or_compute(
        self,
        text: str,
        model_name: str,
        compute_fn: Any,
    ) -> list[float]:
        """Get cached embedding or compute and cache it.

        Uses a per-key asyncio.Lock (singleflight) so that concurrent misses
        for identical content trigger exactly one compute_fn call.  Waiters
        re-check the cache after the winner releases the lock, so they pick up
        the freshly stored value without re-computing.

        Fail-open: when Redis is disabled the lock still serialises concurrent
        computes within the process (preventing redundant CPU/API work), and
        returns the computed value directly.

        Args:
            text: Text to embed.
            model_name: Embedding model name.
            compute_fn: Async function that computes the embedding.
                       Should accept (text) and return numpy array or list[float].

        Returns:
            Embedding as list of floats.
        """
        content_hash = self.hash_content(text)

        # Fast path: cache hit before acquiring any lock.
        cached = await self.get(content_hash, model_name)
        if cached is not None:
            return cached

        # Slow path: acquire the per-key lock to de-duplicate concurrent misses.
        lock_key = f"{model_name}:{content_hash}"
        async with self._inflight.acquire(lock_key):
            # Double-checked read 1: winner may have stored it in Redis.
            cached = await self.get(content_hash, model_name)
            if cached is not None:
                return cached

            # Double-checked read 2: winner may have stored in-memory result
            # (covers the Redis-disabled path where get() always returns None).
            in_mem = self._inflight._results.get(lock_key)
            if in_mem is not None:
                return in_mem

            # We are the winner — compute, store, and return.
            embedding = await compute_fn(text)
            await self.set(content_hash, model_name, embedding)

            result: list[float]
            if hasattr(embedding, "tolist"):
                result = cast("list[float]", embedding.tolist())
            else:
                result = cast("list[float]", list(embedding))

            # Stash for waiters that can't read from Redis (e.g. Redis disabled).
            self._inflight._results[lock_key] = result
            return result
