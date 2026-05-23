"""Tests for CloakBrowserProvider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.content.scraper.cloakbrowser_provider import CloakBrowserProvider

pytestmark = pytest.mark.no_network


def _make_playwright_mocks(
    *,
    html: str = "<html><body><p>" + ("X" * 600) + "</p></body></html>",
    http_status: int | None = 200,
    goto_raises: Exception | None = None,
) -> tuple[MagicMock, MagicMock, MagicMock]:
    """Build the chained async mocks needed to imitate connect_over_cdp -> page.

    Returns (mock_async_playwright_factory, mock_browser, mock_page) so the
    individual assertions can inspect call arguments at each layer.
    """
    page = MagicMock()
    if goto_raises is None:
        response = MagicMock()
        response.status = http_status
        page.goto = AsyncMock(return_value=response)
    else:
        page.goto = AsyncMock(side_effect=goto_raises)
    page.content = AsyncMock(return_value=html)
    page.wait_for_load_state = AsyncMock()
    page.route = AsyncMock()

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()

    chromium = MagicMock()
    chromium.connect_over_cdp = AsyncMock(return_value=browser)

    playwright_instance = MagicMock()
    playwright_instance.chromium = chromium

    async_context = MagicMock()
    async_context.__aenter__ = AsyncMock(return_value=playwright_instance)
    async_context.__aexit__ = AsyncMock(return_value=None)

    factory = MagicMock(return_value=async_context)
    return factory, browser, page


class TestCloakBrowserProvider:
    def test_provider_name(self) -> None:
        provider = CloakBrowserProvider(endpoint_url="http://cloakbrowser:9222")
        assert provider.provider_name == "cloakbrowser"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_successful_scrape_returns_html(self) -> None:
        provider = CloakBrowserProvider(
            endpoint_url="http://cloakbrowser:9222", timeout_sec=5
        )
        factory, browser, page = _make_playwright_mocks()

        with patch(
            "playwright.async_api.async_playwright", factory
        ), patch(
            "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe",
            return_value=(True, None),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.endpoint == "cloakbrowser"
        assert result.http_status == 200
        assert result.content_html is not None
        assert "X" * 600 in result.content_html
        browser.new_context.assert_awaited_once()
        page.goto.assert_awaited_once()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_connects_to_configured_cdp_endpoint(self) -> None:
        provider = CloakBrowserProvider(
            endpoint_url="http://cb-host:9222", timeout_sec=5
        )
        factory, _browser, _page = _make_playwright_mocks()

        with patch(
            "playwright.async_api.async_playwright", factory
        ), patch(
            "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe",
            return_value=(True, None),
        ):
            await provider.scrape_markdown("https://example.com")

        connect_call = factory.return_value.__aenter__.return_value.chromium.connect_over_cdp
        connect_call.assert_awaited_once_with("http://cb-host:9222")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_mobile_flag_drives_context_kwargs(self) -> None:
        provider = CloakBrowserProvider(
            endpoint_url="http://cloakbrowser:9222", timeout_sec=5
        )
        factory, browser, _page = _make_playwright_mocks()

        with patch(
            "playwright.async_api.async_playwright", factory
        ), patch(
            "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe",
            return_value=(True, None),
        ):
            await provider.scrape_markdown("https://example.com", mobile=False)

        kwargs = browser.new_context.await_args.kwargs
        assert kwargs["is_mobile"] is False
        assert kwargs["has_touch"] is False
        assert kwargs["viewport"] == {"width": 1366, "height": 768}

    @pytest.mark.asyncio(loop_scope="function")
    async def test_short_content_returns_error(self) -> None:
        provider = CloakBrowserProvider(
            endpoint_url="http://cloakbrowser:9222",
            timeout_sec=5,
            min_text_length=400,
        )
        factory, _browser, _page = _make_playwright_mocks(
            html="<html><body><p>tiny</p></body></html>"
        )

        with patch(
            "playwright.async_api.async_playwright", factory
        ), patch(
            "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe",
            return_value=(True, None),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "cloakbrowser"
        assert "too short" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_ssrf_preflight_blocks_request(self) -> None:
        provider = CloakBrowserProvider(
            endpoint_url="http://cloakbrowser:9222", timeout_sec=5
        )
        factory, browser, _page = _make_playwright_mocks()

        with patch(
            "playwright.async_api.async_playwright", factory
        ), patch(
            "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe",
            return_value=(False, "private network"),
        ):
            result = await provider.scrape_markdown("http://10.0.0.1/internal")

        assert result.status == "error"
        assert result.endpoint == "cloakbrowser"
        assert "ssrf" in (result.error_text or "").lower()
        # Playwright must not have been touched at all.
        browser.new_context.assert_not_awaited()
        factory.return_value.__aenter__.assert_not_called()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_timeout_returns_error(self) -> None:
        provider = CloakBrowserProvider(
            endpoint_url="http://cloakbrowser:9222", timeout_sec=5
        )
        factory, _browser, _page = _make_playwright_mocks()
        factory.return_value.__aenter__.return_value.chromium.connect_over_cdp.side_effect = (
            TimeoutError("connect timed out")
        )

        with patch(
            "playwright.async_api.async_playwright", factory
        ), patch(
            "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe",
            return_value=(True, None),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "cloakbrowser"
        assert "timeout" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_generic_exception_returns_error(self) -> None:
        provider = CloakBrowserProvider(
            endpoint_url="http://cloakbrowser:9222", timeout_sec=5
        )
        factory, _browser, _page = _make_playwright_mocks()
        factory.return_value.__aenter__.return_value.chromium.connect_over_cdp.side_effect = (
            ConnectionRefusedError("sidecar down")
        )

        with patch(
            "playwright.async_api.async_playwright", factory
        ), patch(
            "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe",
            return_value=(True, None),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "cloakbrowser"
        assert "sidecar down" in (result.error_text or "")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_audit_callback_invoked_on_success(self) -> None:
        audit_calls: list[tuple[str, str, dict[str, Any]]] = []

        def _audit(level: str, event: str, payload: dict[str, Any]) -> None:
            audit_calls.append((level, event, payload))

        provider = CloakBrowserProvider(
            endpoint_url="http://cloakbrowser:9222", timeout_sec=5, audit=_audit
        )
        factory, _browser, _page = _make_playwright_mocks()

        with patch(
            "playwright.async_api.async_playwright", factory
        ), patch(
            "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe",
            return_value=(True, None),
        ):
            await provider.scrape_markdown("https://example.com")

        assert any(event == "cloakbrowser_request" for _, event, _ in audit_calls)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_audit_callback_invoked_on_failure(self) -> None:
        audit_calls: list[tuple[str, str, dict[str, Any]]] = []

        def _audit(level: str, event: str, payload: dict[str, Any]) -> None:
            audit_calls.append((level, event, payload))

        provider = CloakBrowserProvider(
            endpoint_url="http://cloakbrowser:9222", timeout_sec=5, audit=_audit
        )
        factory, _browser, _page = _make_playwright_mocks()
        factory.return_value.__aenter__.return_value.chromium.connect_over_cdp.side_effect = (
            ConnectionRefusedError("sidecar down")
        )

        with patch(
            "playwright.async_api.async_playwright", factory
        ), patch(
            "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe",
            return_value=(True, None),
        ):
            await provider.scrape_markdown("https://example.com")

        assert any(event == "cloakbrowser_failure" for _, event, _ in audit_calls)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_aclose_is_a_noop(self) -> None:
        provider = CloakBrowserProvider(endpoint_url="http://cloakbrowser:9222")
        await provider.aclose()  # Should not raise.
