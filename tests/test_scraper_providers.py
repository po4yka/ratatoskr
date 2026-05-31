"""Tests for individual scraper provider implementations."""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest

from app.adapters.content.scraper.firecrawl_provider import FirecrawlProvider
from tests.helpers.scraper_helpers import _ok_result

# ===================================================================
# FirecrawlProvider tests
# ===================================================================


class TestFirecrawlProvider:
    """Tests for the thin FirecrawlProvider wrapper."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_delegates_to_client_scrape_markdown(self):
        """scrape_markdown forwards the call to the underlying client."""
        mock_client = AsyncMock()
        long_content = "A " * 300  # 600 chars, above default min_content_length
        expected = _ok_result(markdown=long_content)
        mock_client.scrape_markdown.return_value = expected

        provider = FirecrawlProvider(mock_client, name="fc_test")
        result = await provider.scrape_markdown("https://example.com", mobile=False, request_id=7)

        assert result is expected
        mock_client.scrape_markdown.assert_awaited_once_with(
            "https://example.com",
            mobile=False,
            request_id=7,
            wait_for_ms_override=3000,
        )

    def test_provider_name_returns_configured_name(self):
        """provider_name returns the name passed at construction."""
        mock_client = AsyncMock()
        provider = FirecrawlProvider(mock_client, name="firecrawl_self_hosted")
        assert provider.provider_name == "firecrawl_self_hosted"

    def test_provider_name_default(self):
        """Default provider_name is 'firecrawl'."""
        mock_client = AsyncMock()
        provider = FirecrawlProvider(mock_client)
        assert provider.provider_name == "firecrawl"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_aclose_delegates_to_client(self):
        """aclose() calls the client's aclose()."""
        mock_client = AsyncMock()
        provider = FirecrawlProvider(mock_client, name="fc_test")
        await provider.aclose()
        mock_client.aclose.assert_awaited_once()


# ===================================================================
# CrawleeProvider tests
# ===================================================================


class TestScraplingProvider:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_custom_min_content_length_is_honored(self):
        from app.adapters.content.scraper.scrapling_provider import ScraplingProvider

        provider = ScraplingProvider(timeout_sec=5, min_content_length=10)
        with patch.object(
            provider,
            "_fetch",
            new_callable=AsyncMock,
            return_value=("<html><body>tiny</body></html>", "tiny"),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert "insufficient content" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_stealth_launches_are_concurrency_capped(self, monkeypatch):
        """A burst of stealth fallbacks must not exceed the configured cap."""
        from app.adapters.content.scraper import scrapling_provider as sp

        monkeypatch.setenv("SCRAPLING_STEALTH_MAX_CONCURRENCY", "2")
        sp._stealth_semaphores.clear()  # force recreation with the patched cap

        lock = threading.Lock()
        state = {"in_flight": 0, "max_in_flight": 0}

        def _tracking_stealth(url, stealth_cls):
            with lock:
                state["in_flight"] += 1
                state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            try:
                time.sleep(0.05)
            finally:
                with lock:
                    state["in_flight"] -= 1
            long_text = "x" * 500
            return (f"<html><body>{long_text}</body></html>", long_text)

        provider = sp.ScraplingProvider(timeout_sec=5, min_content_length=400)
        provider._stealth_fetcher_cls = object()  # skip the real (heavy) import

        with (
            patch.object(
                provider, "_ensure_async_session", new_callable=AsyncMock, return_value=None
            ),
            patch.object(sp, "_sync_fetch_basic", return_value=("<html></html>", "tiny")),
            patch.object(sp, "_sync_fetch_stealth", side_effect=_tracking_stealth),
        ):
            results = await asyncio.gather(
                *(provider._fetch(f"https://example.com/{i}") for i in range(5))
            )

        assert all(text and len(text) >= 400 for _html, text in results)
        # cap is 2: five concurrent fallbacks never run more than two browsers at once.
        assert state["max_in_flight"] == 2


class TestCrawleeProvider:
    """Tests for the Crawlee hybrid fallback provider."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_beautifulsoup_success_short_circuits_playwright_stage(self):
        """BeautifulSoup stage success should skip Playwright stage."""
        from app.adapters.content.scraper.crawlee_provider import CrawleeProvider

        provider = CrawleeProvider(timeout_sec=5, headless=True, max_retries=2)
        html_body = "<html><body><main>" + ("A" * 500) + "</main></body></html>"

        with (
            patch.object(
                provider,
                "_extract_with_beautifulsoup",
                new_callable=AsyncMock,
                return_value=html_body,
            ) as mock_bs,
            patch.object(provider, "_extract_with_playwright", new_callable=AsyncMock) as mock_pw,
            patch(
                "app.adapters.content.scraper.crawlee_provider.html_to_text",
                return_value="A" * 500,
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.endpoint == "crawlee"
        assert isinstance(result.options_json, dict)
        assert result.options_json.get("stage") == "beautifulsoup"
        mock_bs.assert_awaited_once()
        mock_pw.assert_not_awaited()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_beautifulsoup_thin_then_playwright_success(self):
        """If BeautifulSoup is thin, provider should fallback to Playwright stage."""
        from app.adapters.content.scraper.crawlee_provider import CrawleeProvider

        provider = CrawleeProvider(timeout_sec=5, headless=True, max_retries=2)
        bs_html = "<html><body><p>tiny</p></body></html>"
        pw_html = "<html><body><article>" + ("B" * 500) + "</article></body></html>"

        with (
            patch.object(
                provider,
                "_extract_with_beautifulsoup",
                new_callable=AsyncMock,
                return_value=bs_html,
            ) as mock_bs,
            patch.object(
                provider,
                "_extract_with_playwright",
                new_callable=AsyncMock,
                return_value=pw_html,
            ) as mock_pw,
            patch(
                "app.adapters.content.scraper.crawlee_provider.html_to_text",
                side_effect=lambda html: "tiny" if "tiny" in html else ("B" * 500),
            ),
        ):
            result = await provider.scrape_markdown("https://example.com", mobile=False)

        assert result.status == "ok"
        assert isinstance(result.options_json, dict)
        assert result.options_json.get("stage") == "playwright"
        mock_bs.assert_awaited_once()
        mock_pw.assert_awaited_once()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_both_stages_fail_returns_error(self):
        """If both stages fail to produce content, provider returns error result."""
        from app.adapters.content.scraper.crawlee_provider import CrawleeProvider

        provider = CrawleeProvider(timeout_sec=5)

        with (
            patch.object(
                provider,
                "_extract_with_beautifulsoup",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch.object(
                provider,
                "_extract_with_playwright",
                new_callable=AsyncMock,
                return_value=None,
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "crawlee"
        assert "exhausted" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_timeout_path_returns_error(self):
        """Timeout in both stages should still return a graceful error result."""
        from app.adapters.content.scraper.crawlee_provider import CrawleeProvider

        provider = CrawleeProvider(timeout_sec=1)

        with (
            patch.object(
                provider,
                "_extract_with_beautifulsoup",
                new_callable=AsyncMock,
                side_effect=TimeoutError("bs timeout"),
            ),
            patch.object(
                provider,
                "_extract_with_playwright",
                new_callable=AsyncMock,
                side_effect=TimeoutError("pw timeout"),
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "crawlee"
        assert "timeout" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_custom_min_content_length_is_honored(self):
        from app.adapters.content.scraper.crawlee_provider import CrawleeProvider

        provider = CrawleeProvider(timeout_sec=5, min_content_length=10)
        html_body = "<html><body><article>1234567</article></body></html>"
        with (
            patch.object(
                provider,
                "_extract_with_beautifulsoup",
                new_callable=AsyncMock,
                return_value=html_body,
            ),
            patch.object(
                provider,
                "_extract_with_playwright",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "app.adapters.content.scraper.crawlee_provider.html_to_text",
                return_value="1234567",
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_aclose_is_noop(self):
        """aclose() completes without error (no persistent resources)."""
        from app.adapters.content.scraper.crawlee_provider import CrawleeProvider

        provider = CrawleeProvider()
        await provider.aclose()


# ===================================================================
# PlaywrightProvider tests
# ===================================================================


class TestPlaywrightProvider:
    """Tests for the Playwright browser-rendered fallback provider."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_successful_render_returns_ok(self):
        """A successful rendered fetch with enough content returns status='ok'."""
        from app.adapters.content.scraper.playwright_provider import PlaywrightProvider

        html_body = "<html><body><main>" + ("A" * 500) + "</main></body></html>"
        extracted_text = "A" * 500

        provider = PlaywrightProvider(timeout_sec=5, headless=True)

        with (
            patch.object(provider, "_render_html", new_callable=AsyncMock, return_value=html_body),
            patch(
                "app.adapters.content.scraper.playwright_provider.html_to_text",
                return_value=extracted_text,
            ),
        ):
            result = await provider.scrape_markdown("https://example.com", mobile=True)

        assert result.status == "ok"
        assert result.http_status == 200
        assert result.content_html == html_body
        assert result.endpoint == "playwright"
        assert result.options_json == {"provider": "playwright", "headless": True, "mobile": True}

    @pytest.mark.asyncio(loop_scope="function")
    async def test_timeout_returns_error(self):
        """When render times out, result is an error."""
        from app.adapters.content.scraper.playwright_provider import PlaywrightProvider

        provider = PlaywrightProvider(timeout_sec=1)

        with patch.object(
            provider,
            "_render_html",
            new_callable=AsyncMock,
            side_effect=TimeoutError("timed out"),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert "timeout" in (result.error_text or "").lower()
        assert result.endpoint == "playwright"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_content_too_short_returns_error(self):
        """When extracted text is shorter than threshold, result is an error."""
        from app.adapters.content.scraper.playwright_provider import PlaywrightProvider

        provider = PlaywrightProvider(timeout_sec=5)
        short_html = "<html><body><p>tiny</p></body></html>"

        with (
            patch.object(provider, "_render_html", new_callable=AsyncMock, return_value=short_html),
            patch(
                "app.adapters.content.scraper.playwright_provider.html_to_text",
                return_value="tiny",
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert "too short" in (result.error_text or "").lower()
        assert result.endpoint == "playwright"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_custom_min_text_length_is_honored(self):
        from app.adapters.content.scraper.playwright_provider import PlaywrightProvider

        provider = PlaywrightProvider(timeout_sec=5, min_text_length=5)
        short_html = "<html><body><p>tiny</p></body></html>"

        with (
            patch.object(provider, "_render_html", new_callable=AsyncMock, return_value=short_html),
            patch(
                "app.adapters.content.scraper.playwright_provider.html_to_text",
                return_value="1234",
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert "too short" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_aclose_is_noop(self):
        """aclose() completes without error (no pooled resources)."""
        from app.adapters.content.scraper.playwright_provider import PlaywrightProvider

        provider = PlaywrightProvider()
        await provider.aclose()  # Should not raise


# ===================================================================
# DirectHTMLProvider tests
# ===================================================================


class TestDirectHTMLProvider:
    """Tests for the direct HTML fetch provider."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_successful_fetch_returns_ok(self):
        """A successful HTML fetch with enough content returns status='ok'."""
        from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider

        html_body = "<html><body><p>" + ("A" * 500) + "</p></body></html>"
        extracted_text = "A" * 500

        provider = DirectHTMLProvider(timeout_sec=5)

        with (
            patch.object(provider, "_fetch_html", new_callable=AsyncMock, return_value=html_body),
            patch(
                "app.adapters.content.scraper.direct_html_provider.html_to_text",
                return_value=extracted_text,
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.http_status == 200
        assert result.content_markdown == extracted_text
        assert result.content_html == html_body
        assert result.source_url == "https://example.com"
        assert result.endpoint == "direct_html"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_fetch_with_empty_extracted_text_returns_error(self):
        """Raw HTML must not be accepted when article text extraction is empty."""
        from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider

        html_body = "<html><body><script>window.__APP__ = {}</script></body></html>"
        provider = DirectHTMLProvider(timeout_sec=5)

        with (
            patch.object(provider, "_fetch_html", new_callable=AsyncMock, return_value=html_body),
            patch(
                "app.adapters.content.scraper.direct_html_provider.html_to_text",
                return_value="",
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert "too short" in (result.error_text or "").lower()
        assert result.content_markdown is None
        assert result.content_html == html_body

    @pytest.mark.asyncio(loop_scope="function")
    async def test_non_200_returns_none_html_and_error(self):
        """When _fetch_html returns None (non-200 or non-HTML), result is an error."""
        from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider

        provider = DirectHTMLProvider(timeout_sec=5)

        with patch.object(provider, "_fetch_html", new_callable=AsyncMock, return_value=None):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert "no usable content" in result.error_text

    @pytest.mark.asyncio(loop_scope="function")
    async def test_content_too_short_returns_error(self):
        """When extracted text is shorter than the threshold, result is an error."""
        from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider

        short_html = "<html><body><p>Hi</p></body></html>"
        provider = DirectHTMLProvider(timeout_sec=5)

        with (
            patch.object(provider, "_fetch_html", new_callable=AsyncMock, return_value=short_html),
            patch(
                "app.adapters.content.scraper.direct_html_provider.html_to_text",
                return_value="Hi",
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert "too short" in result.error_text

    @pytest.mark.asyncio(loop_scope="function")
    async def test_custom_min_text_length_is_honored(self):
        from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider

        provider = DirectHTMLProvider(timeout_sec=5, min_text_length=10)
        html_body = "<html><body><p>12345</p></body></html>"

        with (
            patch.object(provider, "_fetch_html", new_callable=AsyncMock, return_value=html_body),
            patch(
                "app.adapters.content.scraper.direct_html_provider.html_to_text",
                return_value="12345",
            ),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert "too short" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_timeout_returns_error(self):
        """When _fetch_html raises a timeout, the result is an error."""
        from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider

        provider = DirectHTMLProvider(timeout_sec=1)

        with patch.object(
            provider,
            "_fetch_html",
            new_callable=AsyncMock,
            side_effect=TimeoutError("timed out"),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert "failed" in result.error_text.lower() or "timed out" in result.error_text.lower()
        assert result.source_url == "https://example.com"
        assert result.endpoint == "direct_html"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_httpx_connect_error_returns_error(self):
        """An httpx connection error is caught and returned as error result."""
        import httpx

        from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider

        provider = DirectHTMLProvider(timeout_sec=1)

        with patch.object(
            provider,
            "_fetch_html",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("connection refused"),
        ):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "direct_html"

    def test_provider_name(self):
        """provider_name is 'direct_html'."""
        from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider

        provider = DirectHTMLProvider()
        assert provider.provider_name == "direct_html"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_aclose_is_noop(self):
        """aclose() completes without error (no resources to release)."""
        from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider

        provider = DirectHTMLProvider()
        await provider.aclose()  # Should not raise
