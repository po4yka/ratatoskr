from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.content.content_extractor import ContentExtractor
from app.adapters.content.quality_filters import detect_low_value_content
from app.adapters.external.firecrawl.client import FirecrawlResult
from app.core.call_status import CallStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from app.config import AppConfig


@asynccontextmanager
async def _dummy_sem() -> AsyncIterator[None]:
    yield


def _make_extractor(
    db: MagicMock, response_formatter: MagicMock, firecrawl: MagicMock
) -> ContentExtractor:
    cfg = cast(
        "AppConfig",
        SimpleNamespace(
            runtime=SimpleNamespace(enable_textacy=False, request_timeout_sec=5),
            redis=SimpleNamespace(
                enabled=False,
                cache_enabled=False,
                prefix="test",
                required=False,
                cache_timeout_sec=0.1,
                firecrawl_ttl_seconds=0,
            ),
        ),
    )
    return ContentExtractor(
        cfg=cfg,
        db=db,
        firecrawl=firecrawl,
        response_formatter=response_formatter,
        audit_func=lambda *args, **kwargs: None,
        sem=_dummy_sem,
    )


def _firecrawl_result(markdown: str | None, html: str | None) -> FirecrawlResult:
    return FirecrawlResult(
        status=CallStatus.OK,
        http_status=200,
        content_markdown=markdown,
        content_html=html,
        structured_json=None,
        metadata_json=None,
        links_json=None,
        response_success=True,
        response_error_code=None,
        response_error_message=None,
        response_details=None,
        latency_ms=123,
        error_text=None,
        source_url="https://example.com",
        endpoint="/v2/scrape",
        options_json={
            "formats": ["markdown", "html"],
            "_chain_attempt_log": [
                {"provider": "direct_html", "status": "success", "latency_ms": 12}
            ],
            "_chain_winning_provider": "direct_html",
        },
        correlation_id="cid-123",
    )


@pytest.mark.asyncio
async def test_low_value_content_triggers_failure() -> None:
    db = MagicMock()
    db.async_update_request_status = AsyncMock()
    # Fix for _execute awaiting _safe_db_operation
    db._safe_db_operation = AsyncMock()
    response_formatter = MagicMock()
    response_formatter.send_firecrawl_start_notification = AsyncMock()
    response_formatter.send_error_notification = AsyncMock()
    response_formatter.send_html_fallback_notification = AsyncMock()
    response_formatter.send_firecrawl_success_notification = AsyncMock()

    firecrawl = MagicMock()
    firecrawl.scrape_markdown = AsyncMock(
        return_value=_firecrawl_result(markdown="Close Close", html="<p>Close</p>")
    )

    extractor = _make_extractor(db, response_formatter, firecrawl)
    cast("Any", extractor)._attempt_direct_html_salvage = AsyncMock(return_value=None)

    # Override internally-created repository with mock to match new Repository pattern
    # The extractor uses message_persistence.request_repo for status updates
    mock_request_repo = MagicMock()
    mock_request_repo.async_update_request_status = AsyncMock()
    extractor.message_persistence.request_repo = mock_request_repo

    mock_crawl_repo = MagicMock()
    mock_crawl_repo.async_insert_crawl_result = AsyncMock(return_value=1)
    extractor.message_persistence.crawl_repo = mock_crawl_repo

    with pytest.raises(ValueError) as exc_info:
        await extractor._perform_new_crawl_with_title(
            message=SimpleNamespace(),
            req_id=42,
            url_text="https://example.com",
            dedupe_hash="hash",
            correlation_id="cid-123",
            interaction_id=None,
            silent=False,
        )

    assert "insufficient_useful_content" in str(exc_info.value)
    mock_request_repo.async_update_request_status.assert_awaited_once_with(42, "error")
    response_formatter.send_error_notification.assert_awaited()
    response_formatter.send_firecrawl_success_notification.assert_not_awaited()

    assert mock_crawl_repo.async_insert_crawl_result.called
    call_kwargs = mock_crawl_repo.async_insert_crawl_result.call_args.kwargs
    # The crawl repo uses 'error' field for error messages
    assert "insufficient_useful_content" in (call_kwargs.get("error") or "")
    assert call_kwargs["attempt_log"] == [
        {"provider": "direct_html", "status": "success", "latency_ms": 12}
    ]
    assert call_kwargs["winning_provider"] == "direct_html"
    assert call_kwargs["options_json"]["_content_quality"]["reason"] == "overlay_content_detected"
    assert call_kwargs["options_json"]["_content_quality"]["winning_provider"] == "direct_html"


def test_detect_nav_stub_content() -> None:
    """Nav stub from TechRadar-like JS-rendered page triggers nav_stub_detected."""
    nav_stub = (
        "[Skip to main content](https://example.com#main)\n\n"
        "Don't miss these\n\n"
        "Close\n\n"
        "Please login or signup to comment\n\n"
        "Please wait...\n\n"
        "Login\n\n"
        "Sign Up"
    )
    result = _firecrawl_result(markdown=nav_stub, html=None)
    issue = detect_low_value_content(result)
    assert issue is not None
    assert issue["reason"] == "nav_stub_detected"


def test_detect_nav_stub_does_not_trigger_on_short_article() -> None:
    """A short but substantive article excerpt should NOT trigger nav_stub_detected."""
    short_article = (
        "The new Sony WH-1000XM5 headphones deliver exceptional noise cancellation "
        "and audio quality that rivals much more expensive alternatives on the market today. "
        "After testing them extensively over three weeks in various environments including "
        "offices, planes, and busy coffee shops, we found the comfort level to be outstanding "
        "for extended listening sessions throughout the entire workday."
    )
    result = _firecrawl_result(markdown=short_article, html=None)
    issue = detect_low_value_content(result)
    assert issue is None


def test_detect_nav_stub_does_not_trigger_on_long_content() -> None:
    """Content with 100+ words should never trigger nav_stub_detected."""
    long_content = " ".join(["word"] * 120) + "."
    result = _firecrawl_result(markdown=long_content, html=None)
    issue = detect_low_value_content(result)
    # Should not trigger nav_stub (word_count >= 100)
    # May or may not trigger other rules, but not nav_stub
    if issue is not None:
        assert issue["reason"] != "nav_stub_detected"


def test_detect_low_value_content_allows_substantive_text() -> None:
    result = _firecrawl_result(
        markdown=(
            "# Heading\n\n"
            "This short article explains the basics of Obsidian vault design. "
            "It covers folder structure, tagging strategies, and linking best practices for new users."
        ),
        html=None,
    )

    assert detect_low_value_content(result) is None
