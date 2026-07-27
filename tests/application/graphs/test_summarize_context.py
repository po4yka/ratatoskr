"""Checkpoint rehydration tests for durable source artifacts."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.graphs.summarize.nodes._context import load_source_text


@pytest.mark.asyncio
async def test_source_rehydration_prefers_checkpoint_artifact_id() -> None:
    crawl_repo = SimpleNamespace(
        async_get_crawl_result_by_id=AsyncMock(
            return_value={
                "id": 17,
                "request_id": 42,
                "content_text": "exact checkpoint source",
            }
        ),
        async_get_crawl_result_by_request=AsyncMock(),
    )
    deps = SimpleNamespace(
        crawl_repo=crawl_repo,
        requests=SimpleNamespace(async_get_request_by_id=AsyncMock()),
    )

    source = await load_source_text(
        {"request_id": 42, "source_artifact_id": 17},  # type: ignore[arg-type]
        deps,  # type: ignore[arg-type]
    )

    assert source == "exact checkpoint source"
    crawl_repo.async_get_crawl_result_by_id.assert_awaited_once_with(17)
    crawl_repo.async_get_crawl_result_by_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_rehydration_rejects_artifact_owned_by_another_request() -> None:
    crawl_repo = SimpleNamespace(
        async_get_crawl_result_by_id=AsyncMock(
            return_value={
                "id": 17,
                "request_id": 99,
                "content_text": "wrong request source",
            }
        ),
        async_get_crawl_result_by_request=AsyncMock(
            return_value={
                "id": 18,
                "request_id": 42,
                "content_text": "request-scoped fallback",
            }
        ),
    )
    deps = SimpleNamespace(
        crawl_repo=crawl_repo,
        requests=SimpleNamespace(async_get_request_by_id=AsyncMock()),
    )

    source = await load_source_text(
        {"request_id": 42, "source_artifact_id": 17},  # type: ignore[arg-type]
        deps,  # type: ignore[arg-type]
    )

    assert source == "request-scoped fallback"
    crawl_repo.async_get_crawl_result_by_request.assert_awaited_once_with(42)
