from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.content.content_extractor import ContentExtractor
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.adapters.telegram import forward_content_processor as fcp
from app.adapters.telegram.forward_content_processor import ForwardContentProcessor
from app.config import AppConfig, RuntimeConfig
from app.core import html_utils
from app.core.call_status import CallStatus
from tests.conftest import make_test_app_config

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )


@asynccontextmanager
async def _dummy_sem():
    yield


def _cfg(enable_textacy: bool) -> AppConfig:
    return make_test_app_config(
        runtime=RuntimeConfig(
            enable_textacy=enable_textacy,
            request_timeout_sec=5,
            preferred_lang="en",
            max_concurrent_calls=1,
        ),
    )


def _response_formatter_stub(**kwargs: object) -> ResponseFormatter:
    return cast("ResponseFormatter", SimpleNamespace(**kwargs))


def _make_content_extractor(
    enable_textacy: bool, response_formatter: ResponseFormatter
) -> ContentExtractor:
    db = MagicMock()
    firecrawl = MagicMock()
    return ContentExtractor(
        cfg=_cfg(enable_textacy),
        db=db,
        firecrawl=firecrawl,
        response_formatter=response_formatter,
        audit_func=lambda *args, **kwargs: None,
        sem=_dummy_sem,
    )


@pytest.mark.asyncio
async def test_reused_crawl_is_normalized_when_flag_enabled() -> None:
    reuse_notifier = AsyncMock()
    response_formatter = _response_formatter_stub(send_content_reuse_notification=reuse_notifier)
    extractor = _make_content_extractor(enable_textacy=True, response_formatter=response_formatter)

    existing_crawl = {
        "content_markdown": "Hello — world https://example.com",
        "content_html": "<p>ignored</p>",
        "status": "ok",
        "http_status": 200,
        "options_json": {"formats": ["markdown"]},
    }

    (
        content_text,
        content_source,
        _title,
        _images,
    ) = await extractor._process_existing_crawl_with_title(
        message=SimpleNamespace(),
        existing_crawl=existing_crawl,
        correlation_id="cid-1",
        silent=False,
    )

    assert content_source == "markdown"
    assert content_text == "Hello - world"
    reuse_notifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_crawl_with_html_fallback_normalizes_text() -> None:
    html_body = "<p>Hello — world</p><p>Visit https://example.com</p>"
    firecrawl_success = AsyncMock()
    html_fallback = AsyncMock()
    response_formatter = _response_formatter_stub(
        send_firecrawl_success_notification=firecrawl_success,
        send_html_fallback_notification=html_fallback,
    )
    extractor = _make_content_extractor(enable_textacy=True, response_formatter=response_formatter)

    crawl = FirecrawlResult(
        status=CallStatus.OK,
        http_status=200,
        content_markdown=None,
        content_html=html_body,
        structured_json=None,
        metadata_json=None,
        links_json=None,
        response_success=True,
        response_error_code=None,
        response_error_message=None,
        response_details=None,
        latency_ms=12,
        error_text=None,
        source_url="https://example.com",
        endpoint="/v2/scrape",
        options_json={"formats": ["html"]},
        correlation_id="cid-2",
    )

    (
        content_text,
        content_source,
        _title,
        _images,
    ) = await extractor._process_successful_crawl_with_title(
        message=SimpleNamespace(), crawl=crawl, correlation_id="cid-2", silent=False
    )

    expected_text = html_utils.normalize_text(html_utils.html_to_text(html_body))
    assert content_source == "html"
    assert content_text == expected_text
    firecrawl_success.assert_awaited_once()
    html_fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_forward_prompt_normalized_when_flag_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    db = MagicMock()
    db.get_request_by_forward.return_value = None
    db.create_request.return_value = 77
    db.update_request_lang_detected = MagicMock()
    db.update_request_correlation_id = MagicMock()
    db.upsert_chat = MagicMock()
    db.upsert_user = MagicMock()
    db.insert_telegram_message = MagicMock()

    forward_accepted = AsyncMock()
    forward_language = AsyncMock()
    response_formatter = _response_formatter_stub(
        send_forward_accepted_notification=forward_accepted,
        send_forward_language_notification=forward_language,
    )

    processor = ForwardContentProcessor(
        cfg=_cfg(enable_textacy=True),
        db=db,
        response_formatter=response_formatter,
        audit_func=lambda *args, **kwargs: None,
    )

    # Override internally-created repository with mock to avoid database proxy issues
    mock_request_repo = MagicMock()
    mock_request_repo.async_get_request_by_forward = AsyncMock(return_value=None)
    mock_request_repo.async_create_request = AsyncMock(return_value=77)
    mock_request_repo.async_update_request_lang_detected = AsyncMock()
    mock_request_repo.async_update_request_correlation_id = AsyncMock()
    mock_request_repo.async_insert_telegram_message = AsyncMock(return_value=1)
    processor.message_persistence.request_repo = mock_request_repo

    # Also mock the chat/user repo methods
    mock_chat_repo = MagicMock()
    mock_chat_repo.async_upsert_chat = AsyncMock()
    processor.message_persistence.chat_repo = mock_chat_repo  # type: ignore[attr-defined]

    mock_user_repo = MagicMock()
    mock_user_repo.async_upsert_user = AsyncMock()
    processor.message_persistence.user_repo = mock_user_repo

    monkeypatch.setattr(fcp, "detect_language", lambda text: "en")
    monkeypatch.setattr(fcp, "choose_language", lambda preferred, detected: detected)

    message = SimpleNamespace(
        text="Hello — world https://example.com",
        chat=SimpleNamespace(id=1, type="private", title="Chat", username="chat"),
        from_user=SimpleNamespace(id=2, username="user"),
        forward_from_chat=SimpleNamespace(id=10, title="Source"),
        forward_from_message_id=55,
        id=99,
        date=1234,
    )

    req_id, prompt, chosen_lang, system_prompt = await processor.process_forward_content(
        message, correlation_id="cid-3"
    )

    assert req_id == 77
    assert prompt == "Channel: Source Hello - world"
    assert chosen_lang == "en"
    assert system_prompt
    mock_request_repo.async_create_request.assert_awaited_once()
    mock_request_repo.async_update_request_lang_detected.assert_awaited_once_with(77, "en")
    forward_accepted.assert_awaited_once()
    forward_language.assert_awaited_once_with(message, "en")
