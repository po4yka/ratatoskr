"""Synchronous embedding bridge for CocoIndex @coco.fn workers.

CocoIndex calls transform functions synchronously from Rust worker threads.
This module spins up a dedicated daemon asyncio loop so we can reuse the
existing async embedding implementations (sentence-transformers, Gemini)
without re-implementing provider switching.

The singleton embedding service and event loop are initialised lazily on
first call and shared across all CocoIndex worker threads.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from app.infrastructure.vector.point_ids import repository_point_id, summary_point_id

_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_service: Any | None = None
_cache: Any | None = None


def _ensure_runtime() -> None:
    global _loop, _loop_thread, _service, _cache
    with _lock:
        if _service is not None:
            return
        from app.config import load_config
        from app.infrastructure.cache.embedding_cache import EmbeddingCache
        from app.infrastructure.cache.redis_cache import RedisCache
        from app.infrastructure.embedding.embedding_factory import create_embedding_service

        cfg = load_config(allow_stub_telegram=True)
        _service = create_embedding_service(cfg.embedding)
        # Redis-backed embedding cache keyed by (model_name, content_hash). When
        # Redis is disabled the cache fails open (always computes), so behavior
        # is unchanged; when enabled, a rescan reuses embeddings already computed
        # for unchanged content instead of re-embedding the whole history.
        _cache = EmbeddingCache(RedisCache(cfg), cfg)
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(
            target=_loop.run_forever,
            daemon=True,
            name="coco-embed-loop",
        )
        _loop_thread.start()


def embed_text_sync(text: str, language: str | None = None) -> list[float]:
    """Generate an embedding vector synchronously via the shared async service.

    Intended to be called from CocoIndex @coco.fn decorated transforms.
    Thread-safe; initialises the daemon loop on first call.
    """
    _ensure_runtime()
    assert _loop is not None  # guaranteed by _ensure_runtime
    assert _service is not None  # guaranteed by _ensure_runtime
    assert _cache is not None  # guaranteed by _ensure_runtime

    model_name = _service.get_model_name(language)

    async def _compute(value: str) -> Any:
        return await _service.generate_embedding(value, language=language, task_type="document")

    fut = asyncio.run_coroutine_threadsafe(
        _cache.get_or_compute(text, model_name, _compute),
        _loop,
    )
    embedding = fut.result(timeout=60.0)
    return embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)


def summary_id_to_point_id(request_id: int, summary_id: int) -> str:
    """Compute the Qdrant point UUID that matches QdrantVectorStore._str_to_uuid.

    Key format must exactly match: f"{request_id}:{summary_id}"
    Namespace must exactly match: uuid.NAMESPACE_OID
    """
    return summary_point_id(request_id, summary_id)


def repository_id_to_point_id(environment: str, user_scope: str, repository_id: int) -> str:
    """Compute the Qdrant point UUID used for repository semantic-search points."""
    return repository_point_id(environment, user_scope, repository_id)
