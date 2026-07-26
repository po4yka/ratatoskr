"""Factory for creating the configured embedding service."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import AppConfig
    from app.config.integrations import EmbeddingConfig
    from app.infrastructure.embedding.embedding_protocol import EmbeddingServiceProtocol

# Process-wide cache of embedding services keyed by config signature.
#
# The local (sentence-transformers) service loads each model lazily and caches
# it per-instance, so a fresh instance per cron run (vector reconcile, x_wiki
# sync) reloads the model from disk every run -- seconds of latency plus the
# memory churn of re-instantiating SentenceTransformer. Caching the service per
# process means the model is loaded once and reused across runs. Nothing in the
# task paths closes the embedding service, so a shared instance is safe.
_CACHE_LOCK = threading.Lock()
_SERVICE_CACHE: dict[tuple[object, ...], EmbeddingServiceProtocol] = {}
# Separate cache for the Redis-cache-wrapped view (opt-in via ``app_config``). The
# heavy model/client stays in the bare _SERVICE_CACHE and is reused as the wrapper's
# inner service; only the cheap CachedEmbeddingService wrapper is cached here.
_CACHED_SERVICE_CACHE: dict[tuple[object, ...], EmbeddingServiceProtocol] = {}


def _cache_key(config: EmbeddingConfig | None) -> tuple[object, ...]:
    """Build a hashable signature of the config fields the factory reads."""
    if config is None or config.provider == "local":
        return ("local",)
    if config.provider == "gemini":
        return (
            "gemini",
            config.gemini_api_key,
            config.gemini_model,
            config.gemini_dimensions,
        )
    if config.provider == "voyage":
        return (
            "voyage",
            config.voyage_api_key,
            config.voyage_model,
            config.voyage_dimensions,
            config.voyage_base_url,
        )
    return (config.provider,)


def create_embedding_service(
    config: EmbeddingConfig | None = None,
    *,
    app_config: AppConfig | None = None,
) -> EmbeddingServiceProtocol:
    """Return an embedding service matching the given configuration.

    When *config* is ``None`` or ``provider == "local"``, the default
    sentence-transformers ``EmbeddingService`` is returned.

    Services are cached per process keyed by config signature so the underlying
    model (local) or remote client (Gemini/Voyage) is built once and reused across task runs.

    When *app_config* is supplied and the Redis embedding cache is enabled
    (``redis.enabled`` and ``redis.cache_enabled``), the service is wrapped in
    :class:`~app.infrastructure.embedding.cached_embedding_service.CachedEmbeddingService`
    so single-text embeds are served from Redis by content hash -- the
    :class:`EmbeddingCache` was implemented but previously never wired in. The heavy
    model/client stays process-cached as the wrapper's inner service. When Redis is
    disabled the bare service is returned unchanged (no behavior change).
    """
    key = _cache_key(config)

    # Wrapped fast path: reuse the per-signature cache-wrapped view.
    if app_config is not None:
        wrapped = _CACHED_SERVICE_CACHE.get(key)
        if wrapped is not None:
            return wrapped
    else:
        cached = _SERVICE_CACHE.get(key)
        if cached is not None:
            return cached

    with _CACHE_LOCK:
        # Double-checked: another thread may have built the bare service meanwhile.
        bare = _SERVICE_CACHE.get(key)
        if bare is None:
            bare = _build_embedding_service(config)
            _SERVICE_CACHE[key] = bare
        if app_config is None:
            return bare
        wrapped = _CACHED_SERVICE_CACHE.get(key)
        if wrapped is not None:
            return wrapped
        maybe_wrapped = _maybe_wrap_with_cache(bare, app_config)
        if maybe_wrapped is None:
            # Redis cache disabled -> return the bare service, no behavior change.
            return bare
        _CACHED_SERVICE_CACHE[key] = maybe_wrapped
        return maybe_wrapped


def _maybe_wrap_with_cache(
    service: EmbeddingServiceProtocol, app_config: AppConfig
) -> EmbeddingServiceProtocol | None:
    """Wrap *service* in ``CachedEmbeddingService`` when the Redis cache is enabled.

    Returns ``None`` when the cache is disabled so the caller returns the bare
    service unchanged (avoids the get_or_compute list-vs-ndarray normalization when
    caching would be a no-op anyway).
    """
    from app.infrastructure.cache.embedding_cache import EmbeddingCache
    from app.infrastructure.cache.redis_cache import RedisCache
    from app.infrastructure.embedding.cached_embedding_service import CachedEmbeddingService

    cache = EmbeddingCache(RedisCache(app_config), app_config)
    if not cache.enabled:
        return None
    return CachedEmbeddingService(service, cache)


def _build_embedding_service(
    config: EmbeddingConfig | None,
) -> EmbeddingServiceProtocol:
    from app.infrastructure.embedding.embedding_service import EmbeddingService

    if config is None or config.provider == "local":
        return EmbeddingService()

    if config.provider == "gemini":
        from app.infrastructure.embedding.gemini_embedding_service import GeminiEmbeddingService

        return GeminiEmbeddingService(
            api_key=config.gemini_api_key,
            model=config.gemini_model,
            dimensions=config.gemini_dimensions,
        )

    if config.provider == "voyage":
        from app.infrastructure.embedding.voyage_embedding_service import VoyageEmbeddingService

        return VoyageEmbeddingService(
            api_key=config.voyage_api_key,
            model=config.voyage_model,
            dimensions=config.voyage_dimensions,
            base_url=config.voyage_base_url,
        )

    msg = f"Unknown embedding provider: {config.provider}"
    raise ValueError(msg)


def reset_embedding_service_cache() -> None:
    """Clear the process-wide embedding-service caches (bare + cache-wrapped)."""
    with _CACHE_LOCK:
        _SERVICE_CACHE.clear()
        _CACHED_SERVICE_CACHE.clear()
