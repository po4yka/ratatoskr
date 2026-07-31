"""Vector search accessors backed by the shared API runtime."""

from __future__ import annotations

from typing import Any

from app.core.logging_utils import get_logger

logger = get_logger(__name__)


async def get_vector_search_service(
    request: Any = None,
) -> Any:
    """FastAPI dependency for the shared vector search service."""
    from app.di.api import ensure_api_runtime, resolve_api_runtime

    runtime = None
    try:
        runtime = resolve_api_runtime(request)
    except RuntimeError:
        runtime = await ensure_api_runtime()

    service = runtime.search.vector_search_service
    if service is not None:
        return service

    msg = "Vector search service is unavailable"
    raise RuntimeError(msg)
