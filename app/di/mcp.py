from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from app.config import DatabaseConfig, load_config
from app.core.embedding_space import resolve_embedding_space_identifier
from app.core.logging_utils import get_logger
from app.db.session import Database
from app.di.types import McpRuntime, McpScope, McpServiceState

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from app.config import AppConfig

VECTOR_RETRY_INTERVAL_SEC = 60.0
LOCAL_VECTOR_RETRY_INTERVAL_SEC = 60.0

logger = get_logger(__name__)


def build_mcp_runtime(
    *,
    database_dsn: str | None = None,
    user_id: int | None = None,
    cfg: AppConfig | None = None,
) -> McpRuntime:
    """Build the MCP runtime with a PostgreSQL database facade and lazy service state."""
    if cfg is None:
        cfg = load_config(allow_stub_telegram=True)
    database_config = DatabaseConfig(dsn=database_dsn) if database_dsn is not None else cfg.database
    database = Database(config=database_config)
    return McpRuntime(
        cfg=cfg,
        database_dsn=database_config.dsn,
        database=database,
        scope=McpScope(user_id=user_id),
    )


def set_mcp_user_scope(runtime: McpRuntime, user_id: int | None) -> None:
    runtime.scope.user_id = user_id


async def _init_lazy_service(
    state: McpServiceState,
    creator: Callable[[], Coroutine[Any, Any, Any]],
    retry_interval: float,
    log_event: str,
) -> Any:
    """Generic double-checked-lock helper for lazy service initialization with retry backoff.

    Args:
        state: Mutable state object tracking the service instance and failure timestamps.
        creator: Async callable that builds and returns the service instance.
        retry_interval: Seconds to wait before retrying after a failed init.
        log_event: Structured log event name emitted on init failure.

    Returns:
        The initialized service, or None if init failed or is within the retry backoff window.
    """
    if state.service is not None:
        return state.service

    now = time.monotonic()
    if state.last_failed_at is not None and (now - state.last_failed_at) < retry_interval:
        return None

    if state.init_lock is None:
        state.init_lock = asyncio.Lock()

    async with state.init_lock:
        if state.service is not None:
            return state.service

        now = time.monotonic()
        if state.last_failed_at is not None and (now - state.last_failed_at) < retry_interval:
            return None

        try:
            state.service = await creator()
            state.last_failed_at = None
            return state.service
        except Exception:
            state.last_failed_at = time.monotonic()
            logger.warning(log_event, exc_info=True, extra={"retry_in_sec": retry_interval})
            return None


async def ensure_mcp_vector_service(runtime: McpRuntime) -> Any:
    """Initialize and cache the MCP vector search service with retry backoff."""

    async def _create() -> Any:
        from app.infrastructure.embedding.embedding_factory import create_embedding_service
        from app.infrastructure.search.vector_search_service import StoreVectorSearchService
        from app.infrastructure.vector.qdrant_store import QdrantVectorStore

        if runtime.cfg is None:
            runtime.cfg = load_config(allow_stub_telegram=True)
        cfg = runtime.cfg.vector_store
        embedding = create_embedding_service(runtime.cfg.embedding)
        store = QdrantVectorStore(
            url=cfg.url,
            api_key=cfg.api_key,
            environment=cfg.environment,
            user_scope=cfg.user_scope,
            collection_version=cfg.collection_version,
            embedding_space=resolve_embedding_space_identifier(runtime.cfg.embedding),
            embedding_dim=runtime.cfg.embedding.embedding_dim,
            required=cfg.required,
            connection_timeout=cfg.connection_timeout,
        )
        runtime.vector_state.resources = (store, embedding)
        return StoreVectorSearchService(
            vector_store=store,
            embedding_service=embedding,
            default_top_k=100,
        )

    return await _init_lazy_service(
        runtime.vector_state,
        _create,
        VECTOR_RETRY_INTERVAL_SEC,
        "mcp_vector_init_failed",
    )


async def ensure_mcp_local_vector_service(runtime: McpRuntime) -> Any:
    """Initialize and cache the local embedding fallback used by MCP tools."""

    async def _create() -> Any:
        from app.infrastructure.embedding.embedding_factory import create_embedding_service

        if runtime.cfg is None:
            runtime.cfg = load_config(allow_stub_telegram=True)
        service = create_embedding_service(runtime.cfg.embedding)
        runtime.local_vector_state.resources = (service,)
        return service

    return await _init_lazy_service(
        runtime.local_vector_state,
        _create,
        LOCAL_VECTOR_RETRY_INTERVAL_SEC,
        "mcp_local_vector_init_failed",
    )


async def close_mcp_runtime(runtime: McpRuntime) -> None:
    """Release lazily-created MCP resources."""
    for state in (runtime.vector_state, runtime.local_vector_state):
        resources = state.resources
        state.service = None
        state.resources = ()
        for resource in resources:
            close = getattr(resource, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    logger.warning("mcp_resource_close_failed", exc_info=True)
    try:
        await runtime.database.dispose()
    except Exception:
        logger.warning("mcp_database_close_failed", exc_info=True)
