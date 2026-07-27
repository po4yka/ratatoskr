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
    source_writer = SimpleNamespace(async_materialize_source_content=AsyncMock(return_value=17))
    return (
        SourceContentBackfillService(
            summary_reader=summary_reader,
            source_extractor=source_extractor,
            source_writer=source_writer,
            persist_source_content=persist_source_content,
        ),
        source_extractor.extract_content_pure,
        source_writer.async_materialize_source_content,
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
    update.assert_awaited_once_with(
        42,
        "Restored article body.",
        source_url="https://example.com/article",
        correlation_id="source-cid",
        content_source="network:markdown",
    )


@pytest.mark.asyncio
async def test_backfill_is_idempotent_when_content_already_exists() -> None:
    context = _context()
    context["crawl_result"] = {"content_text": "Already restored."}
    service, extract, update = _service(context=context)

    result = await service.backfill(user_id=7, summary_id=1402)

    assert result.reextracted is False
    assert result.content_source == "text"
    extract.assert_not_awaited()
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_backfill_does_not_treat_legacy_url_as_source_content() -> None:
    context = _context(content_text="https://example.com/article")
    service, extract, update = _service(context=context)

    result = await service.backfill(user_id=7, summary_id=1402)

    assert result.reextracted is True
    extract.assert_awaited_once()
    update.assert_awaited_once()


@pytest.mark.asyncio
async def test_backfill_materializes_local_raw_content_before_network() -> None:
    context = _context()
    context["crawl_result"] = {
        "content_markdown": (
            "```python\nprint('noise')\n```\n"
            "# Locally preserved [article](https://example.com)\n"
            "![tracking](pixel.png)\n"
            "Share this article\n"
            "Useful paragraph."
        )
    }
    service, extract, update = _service(context=context)

    result = await service.backfill(user_id=7, summary_id=1402)

    assert result.reextracted is False
    assert result.content_source == "markdown"
    extract.assert_not_awaited()
    update.assert_awaited_once_with(
        42,
        "# Locally preserved article\n\nUseful paragraph.",
        source_url="https://example.com/article",
        correlation_id="source-cid",
        content_source="local:markdown",
    )


@pytest.mark.asyncio
async def test_backfill_honors_network_budget_after_local_sources_are_exhausted() -> None:
    service, extract, update = _service(context=_context())

    with pytest.raises(SourceContentBackfillUnavailableError, match="budget"):
        await service.backfill(user_id=7, summary_id=1402, allow_reextract=False)

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
