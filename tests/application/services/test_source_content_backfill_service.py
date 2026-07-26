from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.application.services.source_content_backfill_service import (
    SourceContentBackfillFailedError,
    SourceContentBackfillNotFoundError,
    SourceContentBackfillService,
    SourceContentBackfillUnavailableError,
)


def _context(**request_overrides: object) -> dict[str, object]:
    request = {
        "id": 42,
        "input_url": "https://example.com/article",
        "normalized_url": "https://example.com/article",
        "correlation_id": "source-cid",
        **request_overrides,
    }
    return {
        "request_id": 42,
        "request": request,
        "crawl_result": None,
        "transcription_artifact": None,
    }


def _service(
    *,
    context: dict[str, object] | None,
    extracted: tuple[str, str, dict[str, object]] | None = (
        " Restored article body. ",
        "markdown",
        {},
    ),
    persist_source_content: bool = True,
) -> tuple[SourceContentBackfillService, AsyncMock, AsyncMock]:
    summary_reader = SimpleNamespace(get_summary_context_for_user=AsyncMock(return_value=context))
    source_extractor = SimpleNamespace(
        extract_content_pure=AsyncMock(
            return_value=extracted,
        )
    )
    request_writer = SimpleNamespace(async_update_request_content_text=AsyncMock(return_value=None))
    return (
        SourceContentBackfillService(
            summary_reader=summary_reader,
            source_extractor=source_extractor,
            request_writer=request_writer,
            persist_source_content=persist_source_content,
        ),
        source_extractor.extract_content_pure,
        request_writer.async_update_request_content_text,
    )


@pytest.mark.asyncio
async def test_backfill_reextracts_and_persists_missing_source_content() -> None:
    service, extract, update = _service(context=_context())

    result = await service.backfill(
        user_id=7,
        summary_id=1402,
        operation_correlation_id="operation-cid",
    )

    assert result.reextracted is True
    assert result.content_source == "markdown"
    assert result.content_length == len("Restored article body.")
    extract.assert_awaited_once_with(
        "https://example.com/article",
        correlation_id="source-cid",
        request_id=42,
        update_request_on_failure=False,
    )
    update.assert_awaited_once_with(42, "Restored article body.")


@pytest.mark.asyncio
async def test_backfill_is_idempotent_when_content_already_exists() -> None:
    context = _context(content_text="Already restored.")
    service, extract, update = _service(context=context)

    result = await service.backfill(user_id=7, summary_id=1402)

    assert result.reextracted is False
    assert result.content_source == "text"
    extract.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_rejects_unowned_or_missing_summary() -> None:
    service, _extract, _update = _service(context=None)

    with pytest.raises(SourceContentBackfillNotFoundError):
        await service.backfill(user_id=7, summary_id=1402)


@pytest.mark.asyncio
async def test_backfill_respects_no_retention_mode() -> None:
    service, extract, update = _service(
        context=_context(),
        persist_source_content=False,
    )

    with pytest.raises(SourceContentBackfillUnavailableError, match="retention"):
        await service.backfill(user_id=7, summary_id=1402)

    extract.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_reports_extraction_failure_without_overwriting_request() -> None:
    service, extract, update = _service(context=_context())
    extract.side_effect = ValueError("provider chain exhausted")

    with pytest.raises(SourceContentBackfillFailedError):
        await service.backfill(user_id=7, summary_id=1402)

    update.assert_not_awaited()
