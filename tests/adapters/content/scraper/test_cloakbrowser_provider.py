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
    page.mouse = MagicMock()
    page.mouse.move = AsyncMock()
    page.mouse.wheel = AsyncMock()

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
    @pytest.mark.asyncio(loop_scope="function")
    async def test_successful_scrape_returns_html(self) -> None:
        provider = CloakBrowserProvider(endpoint_url="http://cloakbrowser:9222", timeout_sec=5)
        factory, browser, page = _make_playwright_mocks()

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
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
        provider = CloakBrowserProvider(endpoint_url="http://cb-host:9222", timeout_sec=5)
        factory, _browser, _page = _make_playwright_mocks()

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            await provider.scrape_markdown("https://example.com")

        connect_call = factory.return_value.__aenter__.return_value.chromium.connect_over_cdp
        connect_call.assert_awaited_once()
        called_url = connect_call.await_args.args[0]
        assert called_url.startswith("http://cb-host:9222?")
        assert "fingerprint=" in called_url
        assert "timezone=" in called_url
        assert "locale=" in called_url

    @pytest.mark.asyncio(loop_scope="function")
    async def test_mobile_flag_drives_context_kwargs(self) -> None:
        provider = CloakBrowserProvider(endpoint_url="http://cloakbrowser:9222", timeout_sec=5)
        factory, browser, _page = _make_playwright_mocks()

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
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

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "cloakbrowser"
        assert "too short" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_ssrf_preflight_blocks_request(self) -> None:
        provider = CloakBrowserProvider(endpoint_url="http://cloakbrowser:9222", timeout_sec=5)
        factory, browser, _page = _make_playwright_mocks()

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(False, "private network")),
            ),
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
        provider = CloakBrowserProvider(endpoint_url="http://cloakbrowser:9222", timeout_sec=5)
        factory, _browser, _page = _make_playwright_mocks()
        factory.return_value.__aenter__.return_value.chromium.connect_over_cdp.side_effect = (
            TimeoutError("connect timed out")
        )

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "cloakbrowser"
        assert "timeout" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_generic_exception_returns_error(self) -> None:
        provider = CloakBrowserProvider(endpoint_url="http://cloakbrowser:9222", timeout_sec=5)
        factory, _browser, _page = _make_playwright_mocks()
        factory.return_value.__aenter__.return_value.chromium.connect_over_cdp.side_effect = (
            ConnectionRefusedError("sidecar down")
        )

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
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

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
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

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            await provider.scrape_markdown("https://example.com")

        assert any(event == "cloakbrowser_failure" for _, event, _ in audit_calls)


class TestStealthKnobs:
    def test_fingerprint_seed_is_stable_per_domain(self) -> None:
        from app.adapters.content.scraper.cloakbrowser_provider import _seed_for_url

        seed_a = _seed_for_url("https://example.com/foo")
        seed_b = _seed_for_url("https://example.com/bar?x=1")
        seed_c = _seed_for_url("https://other.example.org/baz")

        assert seed_a == seed_b, "same domain must reuse the same fingerprint seed"
        assert seed_a != seed_c, "different domains must get different seeds"
        assert len(seed_a) == 12 and all(c in "0123456789abcdef" for c in seed_a)

    def test_locale_pool_is_seed_indexed(self) -> None:
        from app.adapters.content.scraper.cloakbrowser_provider import (
            _LOCALE_POOL,
            _locale_for_seed,
            _seed_for_url,
        )

        seed = _seed_for_url("https://example.com/")
        tz, loc = _locale_for_seed(seed)
        assert (tz, loc) in _LOCALE_POOL

    @pytest.mark.asyncio(loop_scope="function")
    async def test_cdp_url_contains_seed_timezone_locale(self) -> None:
        provider = CloakBrowserProvider(endpoint_url="http://cb:9222", timeout_sec=5)
        factory, _browser, _page = _make_playwright_mocks()

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            await provider.scrape_markdown("https://example.com/article")

        from app.adapters.content.scraper.cloakbrowser_provider import (
            _locale_for_seed,
            _seed_for_url,
        )

        expected_seed = _seed_for_url("https://example.com/article")
        expected_tz, expected_loc = _locale_for_seed(expected_seed)

        connect = factory.return_value.__aenter__.return_value.chromium.connect_over_cdp
        called = connect.await_args.args[0]
        assert f"fingerprint={expected_seed}" in called
        # timezone/locale are url-encoded — '/' becomes '%2F', '_' stays
        from urllib.parse import quote

        assert f"timezone={quote(expected_tz, safe='')}" in called
        assert f"locale={quote(expected_loc, safe='')}" in called
        # No proxy when none configured.
        assert "proxy=" not in called

    @pytest.mark.asyncio(loop_scope="function")
    async def test_proxy_appended_when_configured(self) -> None:
        provider = CloakBrowserProvider(
            endpoint_url="http://cb:9222",
            timeout_sec=5,
            proxy="socks5://user:pass@10.0.0.5:1080",
        )
        factory, _browser, _page = _make_playwright_mocks()

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            await provider.scrape_markdown("https://example.com")

        connect = factory.return_value.__aenter__.return_value.chromium.connect_over_cdp
        called = connect.await_args.args[0]
        assert "proxy=socks5%3A%2F%2Fuser%3Apass%4010.0.0.5%3A1080" in called

    @pytest.mark.asyncio(loop_scope="function")
    async def test_in_house_humanize_runs_when_helper_unavailable(self) -> None:
        provider = CloakBrowserProvider(endpoint_url="http://cb:9222", timeout_sec=5, humanize=True)
        factory, _browser, page = _make_playwright_mocks()

        # cloakbrowser package isn't installed in this venv, so the upstream
        # import will fail and the in-house bezier path takes over.
        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert page.mouse.move.await_count >= 1
        assert page.mouse.wheel.await_count >= 1
        assert result.options_json is not None
        assert result.options_json["humanize"] == "in_house"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_humanize_skipped_when_flag_false(self) -> None:
        provider = CloakBrowserProvider(
            endpoint_url="http://cb:9222", timeout_sec=5, humanize=False
        )
        factory, _browser, page = _make_playwright_mocks()

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        page.mouse.move.assert_not_awaited()
        page.mouse.wheel.assert_not_awaited()
        assert result.options_json is not None
        assert result.options_json["humanize"] == "skipped"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_options_json_records_stealth_config_on_success(self) -> None:
        provider = CloakBrowserProvider(
            endpoint_url="http://cb:9222",
            timeout_sec=5,
            humanize=True,
            proxy="http://proxy:3128",
        )
        factory, _browser, _page = _make_playwright_mocks()

        with (
            patch("playwright.async_api.async_playwright", factory),
            patch(
                "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async",
                new=AsyncMock(return_value=(True, None)),
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "ok"
        opts = result.options_json or {}
        assert opts["provider"] == "cloakbrowser"
        assert opts["fingerprint_seed"]
        assert opts["timezone"]
        assert opts["locale"]
        assert opts["humanize"] in {"patched", "in_house"}
        assert opts["proxy_configured"] is True
        # The proxy URL itself must NOT leak into options_json.
        for value in opts.values():
            assert "proxy:3128" not in str(value)
