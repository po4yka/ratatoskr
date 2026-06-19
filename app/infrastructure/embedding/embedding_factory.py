"""Factory for creating the configured embedding service."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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
) -> EmbeddingServiceProtocol:
    """Return an embedding service matching the given configuration.

    When *config* is ``None`` or ``provider == "local"``, the default
    sentence-transformers ``EmbeddingService`` is returned.

    Services are cached per process keyed by config signature so the underlying
    model (local) or remote client (Gemini/Voyage) is built once and reused across task runs.
    """
    key = _cache_key(config)
    cached = _SERVICE_CACHE.get(key)
    if cached is not None:
        return cached

    with _CACHE_LOCK:
        # Double-checked: another thread may have built it while we waited.
        cached = _SERVICE_CACHE.get(key)
        if cached is not None:
            return cached
        service = _build_embedding_service(config)
        _SERVICE_CACHE[key] = service
        return service


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
    """Clear the process-wide embedding-service cache (for tests)."""
    with _CACHE_LOCK:
        _SERVICE_CACHE.clear()
