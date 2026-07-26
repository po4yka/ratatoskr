"""CWE-209 regression: search endpoints must not leak internal exception text.

These call the route handlers directly (no DB / TestClient) so they run without a
Postgres ``TEST_DATABASE_URL``. The end-to-end response-shape tests live in
``test_search_edge_cases.py`` (Postgres-backed, CI only).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.exceptions import ProcessingError
from app.api.routers.content import search as search_router
from app.api.search_helpers import SearchFilters

# A message that mimics a real internal leak: DSN with credentials + internal host.
_SECRET = "postgresql://admin:s3cr3t@internal-db.local:5432/ratatoskr timed out"


def _filters() -> SearchFilters:
    return SearchFilters(
        language=None,
        tags=None,
        domains=None,
        start_date=None,
        end_date=None,
        is_read=None,
        is_favorited=None,
    )


@pytest.mark.asyncio
async def test_search_summaries_handler_hides_internal_exception() -> None:
    service = MagicMock()
    service.search_summaries = AsyncMock(side_effect=RuntimeError(_SECRET))

    with pytest.raises(ProcessingError) as exc_info:
        await search_router.search_summaries(
            q="anything",
            limit=10,
            offset=0,
            mode="auto",
            min_similarity=0.2,
            filters=_filters(),
            user={"user_id": 1},
            search_service=service,
        )

    # Generic, stable label -- and none of the internal exception text.
    assert exc_info.value.message == "Search failed"
    assert _SECRET not in exc_info.value.message
    assert "s3cr3t" not in exc_info.value.message
    assert "internal-db.local" not in exc_info.value.message
    # The original cause is still chained for internal logging/traceback.
    assert isinstance(exc_info.value.__cause__, RuntimeError)


@pytest.mark.asyncio
async def test_semantic_search_handler_hides_internal_exception() -> None:
    service = MagicMock()
    service.semantic_search_summaries = AsyncMock(side_effect=RuntimeError(_SECRET))

    with pytest.raises(ProcessingError) as exc_info:
        await search_router.semantic_search_summaries(
            q="anything",
            limit=10,
            offset=0,
            user_scope=None,
            min_similarity=0.2,
            filters=_filters(),
            user={"user_id": 1},
            search_service=service,
        )

    assert exc_info.value.message == "Semantic search failed"
    assert _SECRET not in exc_info.value.message
    assert "s3cr3t" not in exc_info.value.message
    assert isinstance(exc_info.value.__cause__, RuntimeError)
