"""Tests for PlaywrightProvider content-selector waiting on JS-heavy hosts."""

from __future__ import annotations

import asyncio
import sys
import threading
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from app.core.call_status import CallStatus

_FAKE_ARTICLE_HTML = (
    "<html><body><article><p>"
    "This is a real article with substantial content that should pass "
    "the minimum text length validation check in the playwright provider. "
    "It contains multiple sentences and enough words to be considered valid."
    "</p></article></body></html>"
)


def _setup_playwright_mocks() -> tuple[ModuleType, MagicMock]:
    """Create a mock ``playwright.sync_api`` module and return *(module, page)*."""
    page = MagicMock()
    page.content.return_value = _FAKE_ARTICLE_HTML

    context = MagicMock()
    context.new_page.return_value = page

    browser = MagicMock()
    browser.new_context.return_value = context

    pw_instance = MagicMock()
    pw_instance.chromium.launch.return_value = browser
    pw_instance.__enter__ = MagicMock(return_value=pw_instance)
    pw_instance.__exit__ = MagicMock(return_value=False)

    sync_pw_func = MagicMock(return_value=pw_instance)

    mock_module = ModuleType("playwright.sync_api")
    mock_module.sync_playwright = sync_pw_func  # type: ignore[attr-defined]
    mock_module.Error = type("PlaywrightError", (Exception,), {})  # type: ignore[attr-defined]
    mock_module.TimeoutError = type("PlaywrightTimeoutError", (Exception,), {})  # type: ignore[attr-defined]

    return mock_module, page


def _import_provider(mock_module: ModuleType) -> type:
    """Import PlaywrightProvider with playwright mocked in sys.modules.

    We must invalidate any cached import so the lazy ``from playwright.sync_api``
    inside ``_render_html_sync`` picks up our mock module.
    """
    mod_key = "app.adapters.content.scraper.playwright_provider"
    sys.modules.pop(mod_key, None)

    with patch.dict(
        sys.modules,
        {"playwright": MagicMock(), "playwright.sync_api": mock_module},
    ):
        from app.adapters.content.scraper.playwright_provider import PlaywrightProvider

        return PlaywrightProvider


# -- Tests -------------------------------------------------------------------


@pytest.mark.asyncio(loop_scope="function")
async def test_waits_for_article_selector_on_js_heavy_host() -> None:
    """When URL matches a JS-heavy host, wait_for_selector is called."""
    mock_module, page_mock = _setup_playwright_mocks()
    Provider = _import_provider(mock_module)

    with patch.dict(
        sys.modules,
        {"playwright": MagicMock(), "playwright.sync_api": mock_module},
    ):
        provider = Provider(
            timeout_sec=30,
            min_text_length=10,
            js_heavy_hosts=("techradar.com",),
        )
        await provider.scrape_markdown("https://www.techradar.com/some-article")

    page_mock.wait_for_selector.assert_called_once()
    selector_arg = page_mock.wait_for_selector.call_args[0][0]
    assert "article p" in selector_arg
    assert page_mock.wait_for_selector.call_args[1]["timeout"] == 8_000


@pytest.mark.asyncio(loop_scope="function")
async def test_skips_selector_wait_for_normal_urls() -> None:
    """When URL does NOT match JS-heavy hosts, wait_for_selector is not called."""
    mock_module, page_mock = _setup_playwright_mocks()
    Provider = _import_provider(mock_module)

    with patch.dict(
        sys.modules,
        {"playwright": MagicMock(), "playwright.sync_api": mock_module},
    ):
        provider = Provider(
            timeout_sec=30,
            min_text_length=10,
            js_heavy_hosts=("techradar.com",),
        )
        await provider.scrape_markdown("https://www.example.com/article")

    page_mock.wait_for_selector.assert_not_called()


@pytest.mark.asyncio(loop_scope="function")
async def test_selector_timeout_does_not_fail_provider() -> None:
    """If wait_for_selector times out, provider still returns content."""
    mock_module, page_mock = _setup_playwright_mocks()
    page_mock.wait_for_selector.side_effect = mock_module.TimeoutError("timeout")

    Provider = _import_provider(mock_module)

    with patch.dict(
        sys.modules,
        {"playwright": MagicMock(), "playwright.sync_api": mock_module},
    ):
        provider = Provider(
            timeout_sec=30,
            min_text_length=10,
            js_heavy_hosts=("techradar.com",),
        )
        result = await provider.scrape_markdown("https://www.techradar.com/some-article")

    assert result.content_html is not None
    assert result.status == CallStatus.OK


@pytest.mark.asyncio(loop_scope="function")
async def test_skips_selector_wait_when_no_js_heavy_hosts_configured() -> None:
    """When js_heavy_hosts is empty, wait_for_selector is never called."""
    mock_module, page_mock = _setup_playwright_mocks()
    Provider = _import_provider(mock_module)

    with patch.dict(
        sys.modules,
        {"playwright": MagicMock(), "playwright.sync_api": mock_module},
    ):
        provider = Provider(
            timeout_sec=30,
            min_text_length=10,
            js_heavy_hosts=(),
        )
        await provider.scrape_markdown("https://www.techradar.com/some-article")

    page_mock.wait_for_selector.assert_not_called()


@pytest.mark.asyncio(loop_scope="function")
async def test_aborted_render_stops_before_capturing_content() -> None:
    """A cancelled scrape must stop the browser early, not burn its timeout budget.

    The chain cancels every loser of the browser-tier race, so without a
    cooperative check the orphaned Chromium kept scrolling and waiting for
    networkidle for the full budget while nobody wanted the result.
    """
    mock_module, page_mock = _setup_playwright_mocks()
    Provider = _import_provider(mock_module)

    with patch.dict(
        sys.modules,
        {"playwright": MagicMock(), "playwright.sync_api": mock_module},
    ):
        provider = Provider(timeout_sec=30, min_text_length=10)
        abort = threading.Event()
        abort.set()

        html = provider._render_html_sync("https://example.com/article", abort=abort)

    assert html is None
    page_mock.content.assert_not_called()
    page_mock.evaluate.assert_not_called()
    page_mock.close.assert_called_once()


@pytest.mark.asyncio(loop_scope="function")
async def test_cancelling_render_signals_the_worker_thread() -> None:
    """_render_html must flip the abort flag the sync body polls."""
    mock_module, _page = _setup_playwright_mocks()
    Provider = _import_provider(mock_module)

    entered = threading.Event()
    observed: list[threading.Event] = []

    def _slow_render(_url: str, **kwargs: object) -> None:
        abort = kwargs["abort"]
        assert isinstance(abort, threading.Event)
        observed.append(abort)
        entered.set()
        abort.wait(timeout=5)

    with patch.dict(
        sys.modules,
        {"playwright": MagicMock(), "playwright.sync_api": mock_module},
    ):
        provider = Provider(timeout_sec=30, min_text_length=10)
        provider._render_html_sync = _slow_render  # type: ignore[method-assign]

        task = asyncio.create_task(provider._render_html("https://example.com/article"))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert observed, "the sync body never received an abort flag"
    assert observed[0].is_set(), (
        "cancelling the scrape left the flag clear, so the browser runs on unwatched"
    )
