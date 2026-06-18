from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.adapter_models.batch_processing import URLBatchStatus
from app.adapters.telegram.url_batch_processor import (
    BatchProcessRequest,
    URLBatchProcessor,
    _BatchRunState,
)
from app.core.url_utils import compute_dedupe_hash, normalize_url


def _make_processor(
    *, request_repo: Any | None = None, user_repo: Any | None = None
) -> URLBatchProcessor:
    response_formatter = SimpleNamespace(
        safe_reply=AsyncMock(),
        safe_reply_with_id=AsyncMock(return_value=7),
        send_cached_summary_notification=AsyncMock(),
        send_structured_summary_response=AsyncMock(),
        edit_message=AsyncMock(return_value=True),
        sender=None,
    )
    return URLBatchProcessor(
        response_formatter=response_formatter,
        request_repo=request_repo
        or SimpleNamespace(
            async_get_request_by_dedupe_hash=AsyncMock(return_value=None),
            async_create_minimal_request=AsyncMock(return_value=(1, True)),
            async_update_request_error=AsyncMock(),
        ),
        user_repo=user_repo or SimpleNamespace(async_update_user_interaction=AsyncMock()),
        summary_repo=SimpleNamespace(async_get_summary_by_request=AsyncMock(return_value=None)),
        relationship_analysis_service=None,
    )


@pytest.mark.asyncio
async def test_cache_hit_delivers_cached_summary_without_new_request() -> None:
    request_repo = SimpleNamespace(
        async_get_request_by_dedupe_hash=AsyncMock(return_value={"id": 5, "status": "ok"}),
        async_create_minimal_request=AsyncMock(),
        async_update_request_error=AsyncMock(),
    )
    processor = _make_processor(request_repo=request_repo)
    processor._summary_repo.async_get_summary_by_request = AsyncMock(
        return_value={"json_payload": {"title": "Cached article", "summary_250": "Cached summary"}}
    )

    request = BatchProcessRequest(
        message=SimpleNamespace(chat=SimpleNamespace(id=1)),
        urls=["https://example.com/cached"],
        uid=1,
        correlation_id="cid",
        handle_single_url=AsyncMock(),
    )

    with (
        patch("app.adapters.telegram.url_batch_processor.asyncio.sleep", new=AsyncMock()),
        patch.object(processor, "_progress_heartbeat", new=AsyncMock()),
    ):
        result = await processor.execute_batch(request)

    assert result is not None
    assert result.url_to_request_id["https://example.com/cached"] == 5
    request_repo.async_create_minimal_request.assert_not_called()
    processor._response_formatter.send_cached_summary_notification.assert_awaited_once()  # type: ignore[attr-defined]
    processor._response_formatter.send_structured_summary_response.assert_awaited_once()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_progress_edit_circuit_breaker_stops_after_three_failures() -> None:
    processor = _make_processor()
    processor._response_formatter.edit_message = AsyncMock(return_value=False)  # type: ignore[method-assign]
    request = BatchProcessRequest(
        message=SimpleNamespace(chat=SimpleNamespace(id=1)),
        urls=["https://example.com/1"],
        uid=1,
        correlation_id="cid",
        initial_message_id=7,
    )
    state = _BatchRunState(
        request=request,
        batch_status=URLBatchStatus.from_urls(request.urls),
        url_to_request_id={},
        cached_summaries=[],
        semaphore=AsyncMock(),
        sender=processor._response_formatter,
        draft_enabled=False,
        initial_message_id=7,
    )

    for _ in range(4):
        await processor._format_progress_message(state, 1, 1, 7)

    assert processor._response_formatter.edit_message.await_count == 3


@pytest.mark.asyncio
async def test_batch_completion_updates_interaction_once_processing_finishes() -> None:
    user_repo = SimpleNamespace(async_update_user_interaction=AsyncMock())
    request_repo = SimpleNamespace(
        async_get_request_by_dedupe_hash=AsyncMock(return_value=None),
        async_create_minimal_request=AsyncMock(return_value=(11, True)),
        async_update_request_error=AsyncMock(),
    )
    processor = _make_processor(request_repo=request_repo, user_repo=user_repo)
    handle_single_url = AsyncMock(return_value=SimpleNamespace(title="Processed"))

    request = BatchProcessRequest(
        message=SimpleNamespace(chat=SimpleNamespace(id=1)),
        urls=["https://example.com/one"],
        uid=9,
        correlation_id="cid",
        interaction_id=77,
        start_time=time.time() - 1,
        handle_single_url=handle_single_url,
    )

    with (
        patch("app.adapters.telegram.url_batch_processor.asyncio.sleep", new=AsyncMock()),
        patch.object(processor, "_progress_heartbeat", new=AsyncMock()),
    ):
        await processor.execute_batch(request)

    user_repo.async_update_user_interaction.assert_awaited_once()
    _, kwargs = user_repo.async_update_user_interaction.await_args
    assert kwargs["interaction_id"] == 77
    assert kwargs["response_type"] == "batch_complete"


@pytest.mark.asyncio
async def test_batch_pre_registration_uses_normalized_dedupe_hash() -> None:
    request_repo = SimpleNamespace(
        async_get_request_by_dedupe_hash=AsyncMock(return_value=None),
        async_find_recent_request_by_dedupe=AsyncMock(return_value=None),
        async_create_minimal_request=AsyncMock(return_value=(12, True)),
        async_update_request_error=AsyncMock(),
    )
    processor = _make_processor(request_repo=request_repo)
    url = "HTTPS://Example.com/articles/one?utm_source=newsletter"
    request = BatchProcessRequest(
        message=SimpleNamespace(chat=SimpleNamespace(id=1)),
        urls=[url],
        uid=9,
        correlation_id="cid",
        handle_single_url=AsyncMock(),
    )
    state = _BatchRunState(
        request=request,
        batch_status=URLBatchStatus.from_urls(request.urls),
        url_to_request_id={},
        cached_summaries=[],
        semaphore=AsyncMock(),
        sender=processor._response_formatter,
        draft_enabled=False,
    )

    await processor._pre_register_urls(state)

    normalized = normalize_url(url)
    expected_hash = compute_dedupe_hash(normalized)
    request_repo.async_get_request_by_dedupe_hash.assert_awaited_once_with(expected_hash)
    request_repo.async_find_recent_request_by_dedupe.assert_awaited_once_with(
        expected_hash, max_age_sec=60
    )
    _, kwargs = request_repo.async_create_minimal_request.await_args
    assert kwargs["normalized_url"] == normalized
    assert kwargs["dedupe_hash"] == expected_hash
