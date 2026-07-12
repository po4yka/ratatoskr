"""Caching decorator that wires :class:`EmbeddingCache` into an embedding service.

``EmbeddingCache`` (Redis-backed, content-hash keyed) was fully implemented but
never wired into any call site. ``CachedEmbeddingService`` is the seam: it wraps
any :class:`~app.infrastructure.embedding.embedding_protocol.EmbeddingServiceProtocol`
so single-text embeds are served from Redis by content hash, and delegates
everything else unchanged. The embedding factory applies it when a Redis cache is
available (see :func:`create_embedding_service`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.infrastructure.cache.embedding_cache import EmbeddingCache
    from app.infrastructure.embedding.embedding_protocol import EmbeddingServiceProtocol


class CachedEmbeddingService:
    """Serve single-text embeddings from :class:`EmbeddingCache`, delegating the rest.

    Decorator (not a subclass) over any ``EmbeddingServiceProtocol``:

    - ``generate_embedding`` routes through ``EmbeddingCache.get_or_compute`` --
      content-hash keyed, partitioned by the inner service's model name, with the
      cache's built-in per-key singleflight and fail-open-when-Redis-disabled
      behavior. The ``compute_fn`` is the inner service's own ``generate_embedding``,
      so a miss recomputes exactly as before and stores the result.
    - ``generate_embeddings_batch`` delegates to the inner service so remote
      providers keep their real batch API call (a naive fan-out through the cache
      would turn one batch request into N single requests). The consumers that
      benefit from caching all use the single-text path.
    - serialize / deserialize / model / dimension / close all delegate unchanged.

    ``get_or_compute`` returns ``list[float]``; the inner service may return a numpy
    array, but every consumer normalizes via the ``hasattr(x, "tolist")`` guard, so
    the list return is contract-compatible.
    """

    def __init__(self, inner: EmbeddingServiceProtocol, cache: EmbeddingCache) -> None:
        self._inner = inner
        self._cache = cache

    @property
    def inner(self) -> EmbeddingServiceProtocol:
        """The wrapped service (exposed for introspection / tests)."""
        return self._inner

    async def generate_embedding(
        self, text: str, *, language: str | None = None, task_type: str | None = None
    ) -> Any:
        # Partition the cache by the inner service's resolved model so a model or
        # language switch never returns a stale vector. get_model_name is a cheap
        # config lookup (no model load), safe to call on every request incl. hits.
        model_name = self._inner.get_model_name(language)

        async def _compute(value: str) -> Any:
            return await self._inner.generate_embedding(
                value, language=language, task_type=task_type
            )

        return await self._cache.get_or_compute(text, model_name, _compute)

    async def generate_embeddings_batch(
        self,
        texts: Sequence[str],
        *,
        language: str | None = None,
        task_type: str | None = None,
    ) -> list[Any]:
        # Delegate to the inner service (preserves remote providers' batch API and
        # the local provider's own fan-out). Only the single-text path is cached.
        return await self._inner.generate_embeddings_batch(
            texts, language=language, task_type=task_type
        )

    def serialize_embedding(self, embedding: Any) -> bytes:
        return self._inner.serialize_embedding(embedding)

    def deserialize_embedding(self, blob: bytes) -> list[float]:
        return self._inner.deserialize_embedding(blob)

    def get_model_name(self, language: str | None = None) -> str:
        return self._inner.get_model_name(language)

    def get_dimensions(self, language: str | None = None) -> int:
        return self._inner.get_dimensions(language)

    async def get_dimensions_async(self, language: str | None = None) -> int:
        return await self._inner.get_dimensions_async(language)

    def close(self) -> None:
        self._inner.close()

    async def aclose(self) -> None:
        await self._inner.aclose()


__all__ = ["CachedEmbeddingService"]
