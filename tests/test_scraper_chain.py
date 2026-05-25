"""Tests for the ContentScraperChain ordered-fallback logic."""

from __future__ import annotations

import pytest

from app.adapters.content.scraper.chain import ContentScraperChain
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from tests.helpers.scraper_helpers import _error_result, _MockProvider, _ok_result

# ===================================================================
# ContentScraperChain tests
# ===================================================================


class TestContentScraperChain:
    """Tests for the ordered-fallback ContentScraperChain."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_first_provider_succeeds_second_not_called(self):
        """When the first provider returns OK, the second is never invoked."""
        p1 = _MockProvider(name="first", result=_ok_result())
        p2 = _MockProvider(name="second", result=_ok_result(markdown="# Second"))

        chain = ContentScraperChain([p1, p2])
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == "# OK"
        assert len(p1.calls) == 1
        assert len(p2.calls) == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_first_fails_second_succeeds(self):
        """When the first provider returns an error result, the chain tries the second."""
        p1 = _MockProvider(name="first", result=_error_result())
        p2 = _MockProvider(name="second", result=_ok_result(markdown="# Fallback"))

        chain = ContentScraperChain([p1, p2])
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == "# Fallback"
        assert len(p1.calls) == 1
        assert len(p2.calls) == 1

    @pytest.mark.asyncio(loop_scope="function")
    async def test_first_raises_exception_second_succeeds(self):
        """When the first provider raises, the chain catches and tries the next."""
        p1 = _MockProvider(name="first", exception=RuntimeError("boom"))
        p2 = _MockProvider(name="second", result=_ok_result(markdown="# Recovered"))

        chain = ContentScraperChain([p1, p2])
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == "# Recovered"
        assert len(p1.calls) == 1
        assert len(p2.calls) == 1

    @pytest.mark.asyncio(loop_scope="function")
    async def test_all_providers_fail_returns_aggregate_error(self):
        """When every provider fails, the chain returns all provider failure reasons."""
        p1 = _MockProvider(name="first", result=_error_result(error="p1 fail"))
        p2 = _MockProvider(name="second", result=_error_result(error="p2 fail"))

        chain = ContentScraperChain([p1, p2])
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.error_text is not None
        assert "All providers failed" in result.error_text
        assert "first: p1 fail" in result.error_text
        assert "second: p2 fail" in result.error_text
        assert result.source_url == "https://example.com"
        assert result.endpoint == "chain"
        assert len(p1.calls) == 1
        assert len(p2.calls) == 1

    @pytest.mark.asyncio(loop_scope="function")
    async def test_all_providers_raise_returns_synthetic_error(self):
        """When every provider raises an exception, the chain returns a synthetic error."""
        p1 = _MockProvider(name="first", exception=RuntimeError("err1"))
        p2 = _MockProvider(name="second", exception=ValueError("err2"))

        chain = ContentScraperChain([p1, p2])
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert "All providers failed" in result.error_text
        assert "first: err1" in result.error_text
        assert "second: err2" in result.error_text
        assert result.source_url == "https://example.com"
        assert result.endpoint == "chain"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_single_provider_chain_success(self):
        """A chain with a single provider works correctly on success."""
        p = _MockProvider(name="solo", result=_ok_result(markdown="# Solo"))
        chain = ContentScraperChain([p])
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == "# Solo"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_single_provider_chain_failure(self):
        """A chain with one failing provider still reports the aggregate chain error."""
        p = _MockProvider(name="solo", result=_error_result(error="solo fail"))
        chain = ContentScraperChain([p])
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.error_text is not None
        assert "All providers failed" in result.error_text
        assert "solo: solo fail" in result.error_text

    @pytest.mark.asyncio(loop_scope="function")
    async def test_low_value_ok_content_falls_through_to_next_provider(self):
        """Long OK content that is low-value should not stop the fallback chain."""
        repeated_stub = "cookie " * 120
        p1 = _MockProvider(name="first", result=_ok_result(markdown=repeated_stub))
        fallback_content = (
            "This fallback provider returned a useful article with enough body text, "
            "context, and complete sentences for downstream extraction. " * 3
        )
        p2 = _MockProvider(name="second", result=_ok_result(markdown=fallback_content))

        chain = ContentScraperChain([p1, p2], min_content_length=100)
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == fallback_content
        assert len(p1.calls) == 1
        assert len(p2.calls) == 1

    @pytest.mark.asyncio(loop_scope="function")
    async def test_html_candidate_can_rescue_low_value_markdown(self):
        """Rich HTML should be considered before rejecting thin or low-value markdown."""
        thin_markdown = "cookie cookie cookie"
        rich_html = (
            "<article><p>"
            + (
                "This article contains detailed reporting with enough context and supporting "
                "sentences to be treated as useful extracted content. " * 8
            )
            + "</p></article>"
        )
        p1 = _MockProvider(
            name="first",
            result=FirecrawlResult(
                status=CallStatus.OK,
                http_status=200,
                content_markdown=thin_markdown,
                content_html=rich_html,
                source_url="https://example.com",
                endpoint="mock",
            ),
        )
        p2 = _MockProvider(name="second", result=_ok_result(markdown="# Should not be used"))

        chain = ContentScraperChain([p1, p2], min_content_length=400)
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == thin_markdown
        assert result.content_html == rich_html
        assert len(p1.calls) == 1
        assert len(p2.calls) == 0

    def test_empty_providers_raises_value_error(self):
        """Constructing a chain with no providers raises ValueError."""
        with pytest.raises(ValueError, match="at least one provider"):
            ContentScraperChain([])

    @pytest.mark.asyncio(loop_scope="function")
    async def test_aclose_closes_all_providers(self):
        """aclose() calls aclose() on every provider, even if one raises."""
        p1 = _MockProvider(name="first", result=_ok_result())
        p2 = _MockProvider(name="second", result=_ok_result())

        chain = ContentScraperChain([p1, p2])
        await chain.aclose()

        assert p1.closed is True
        assert p2.closed is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_aclose_tolerates_provider_error(self):
        """aclose() does not propagate if a provider's aclose raises."""

        class _FailClose(_MockProvider):
            async def aclose(self) -> None:
                raise RuntimeError("close error")

        p1 = _FailClose(name="fail_close", result=_ok_result())
        p2 = _MockProvider(name="ok_close", result=_ok_result())

        chain = ContentScraperChain([p1, p2])
        await chain.aclose()  # Should not raise

        assert p2.closed is True

    @pytest.mark.asyncio(loop_scope="function")
    async def test_audit_callback_invoked_on_success(self):
        """The optional audit callback is fired when a provider succeeds."""
        audit_calls: list[tuple[str, str, dict]] = []

        def audit(level: str, event: str, data: dict) -> None:
            audit_calls.append((level, event, data))

        p = _MockProvider(name="audited", result=_ok_result())
        chain = ContentScraperChain([p], audit=audit)
        await chain.scrape_markdown("https://example.com", request_id=42)

        assert len(audit_calls) == 1
        level, event, data = audit_calls[0]
        assert level == "INFO"
        assert event == "scraper_chain_success"
        assert data["provider"] == "audited"
        assert data["url"] == "https://example.com/[redacted]"
        assert data["request_id"] == 42

    @pytest.mark.asyncio(loop_scope="function")
    async def test_audit_callback_not_invoked_on_failure(self):
        """The audit callback is not fired when all providers fail."""
        audit_calls: list[tuple[str, str, dict]] = []

        def audit(level: str, event: str, data: dict) -> None:
            audit_calls.append((level, event, data))

        p = _MockProvider(name="fail", result=_error_result())
        chain = ContentScraperChain([p], audit=audit)
        await chain.scrape_markdown("https://example.com")

        assert len(audit_calls) == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_provider_name_is_chain(self):
        """The chain's own provider_name is 'chain'."""
        p = _MockProvider(name="inner", result=_ok_result())
        chain = ContentScraperChain([p])
        assert chain.provider_name == "chain"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_ok_status_but_empty_content_treated_as_failure(self):
        """A result with status='ok' but no content is treated as a failure."""
        empty_ok = FirecrawlResult(
            status="ok",
            http_status=200,
            content_markdown="",
            content_html=None,
            source_url="https://example.com",
            endpoint="mock",
        )
        p1 = _MockProvider(name="empty", result=empty_ok)
        p2 = _MockProvider(name="good", result=_ok_result(markdown="# Content"))

        chain = ContentScraperChain([p1, p2])
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == "# Content"
        assert len(p1.calls) == 1
        assert len(p2.calls) == 1

    @pytest.mark.asyncio(loop_scope="function")
    async def test_passes_mobile_and_request_id_to_providers(self):
        """The chain forwards mobile and request_id kwargs to providers."""
        p = _MockProvider(name="check", result=_ok_result())
        chain = ContentScraperChain([p])
        await chain.scrape_markdown("https://example.com", mobile=False, request_id=99)

        assert p.calls[0]["mobile"] is False
        assert p.calls[0]["request_id"] == 99

    @pytest.mark.asyncio(loop_scope="function")
    async def test_playwright_success_stops_before_direct_html(self):
        """Legacy serial mode: Playwright succeeds, lower-priority direct_html is not invoked."""
        scrapling = _MockProvider(name="scrapling", result=_error_result(error="scrapling failed"))
        firecrawl = _MockProvider(name="firecrawl", result=_error_result(error="firecrawl failed"))
        playwright = _MockProvider(name="playwright", result=_ok_result(markdown="# Rendered"))
        direct_html = _MockProvider(name="direct_html", result=_ok_result(markdown="# Direct"))

        chain = ContentScraperChain(
            [scrapling, firecrawl, playwright, direct_html], race_enabled=False
        )
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == "# Rendered"
        assert len(scrapling.calls) == 1
        assert len(firecrawl.calls) == 1
        assert len(playwright.calls) == 1
        assert len(direct_html.calls) == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_crawlee_success_stops_before_direct_html(self):
        """Legacy serial mode: Crawlee succeeds, direct_html is not invoked."""
        scrapling = _MockProvider(name="scrapling", result=_error_result(error="scrapling failed"))
        firecrawl = _MockProvider(name="firecrawl", result=_error_result(error="firecrawl failed"))
        playwright = _MockProvider(
            name="playwright", result=_error_result(error="playwright failed")
        )
        crawlee = _MockProvider(name="crawlee", result=_ok_result(markdown="# Crawlee"))
        direct_html = _MockProvider(name="direct_html", result=_ok_result(markdown="# Direct"))

        chain = ContentScraperChain(
            [scrapling, firecrawl, playwright, crawlee, direct_html], race_enabled=False
        )
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == "# Crawlee"
        assert len(scrapling.calls) == 1
        assert len(firecrawl.calls) == 1
        assert len(playwright.calls) == 1
        assert len(crawlee.calls) == 1
        assert len(direct_html.calls) == 0


# ===================================================================
# Chain-level min_content_length tests
# ===================================================================


class TestChainMinContentLength:
    """Tests for chain-level content length enforcement."""

    @pytest.mark.asyncio(loop_scope="function")
    async def test_chain_rejects_thin_content_and_falls_through(self):
        """Chain with min_content_length rejects short OK result, tries next provider."""
        thin = _ok_result(markdown="nav stub only")
        good_content = (
            "This fallback article contains useful context, complete sentences, "
            "and enough distinct words to pass the content quality guard. " * 5
        )
        good = _ok_result(markdown=good_content)

        p1 = _MockProvider(name="firecrawl", result=thin)
        p2 = _MockProvider(name="playwright", result=good)

        chain = ContentScraperChain([p1, p2], min_content_length=400)
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == good.content_markdown
        assert len(p1.calls) == 1
        assert len(p2.calls) == 1

    @pytest.mark.asyncio(loop_scope="function")
    async def test_chain_accepts_sufficient_content(self):
        """Chain with min_content_length accepts result meeting threshold."""
        good_content = (
            "This article contains useful context, complete sentences, and enough "
            "distinct words to pass the content quality guard. " * 5
        )
        good = _ok_result(markdown=good_content)

        p1 = _MockProvider(name="firecrawl", result=good)
        p2 = _MockProvider(name="playwright", result=_ok_result())

        chain = ContentScraperChain([p1, p2], min_content_length=400)
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == good.content_markdown
        assert len(p2.calls) == 0  # Not reached

    @pytest.mark.asyncio(loop_scope="function")
    async def test_chain_default_zero_accepts_any_content(self):
        """Chain with default min_content_length=0 accepts any non-empty content."""
        short = _ok_result(markdown="# OK")

        p1 = _MockProvider(name="first", result=short)
        chain = ContentScraperChain([p1])  # default min_content_length=0
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == "# OK"


# ---------------------------------------------------------------------------
# JS-heavy host reordering
# ---------------------------------------------------------------------------


class TestJsHeavyReordering:
    """Chain should try browser providers first for JS-heavy hosts."""

    @pytest.mark.asyncio
    async def test_chain_reorders_for_js_heavy_url(self) -> None:
        scrapling = _MockProvider(name="scrapling", result=_ok_result())
        playwright = _MockProvider(name="playwright", result=_ok_result())

        chain = ContentScraperChain(
            [scrapling, playwright],
            js_heavy_hosts=("techradar.com",),
        )
        result = await chain.scrape_markdown("https://www.techradar.com/article")

        assert result.status == CallStatus.OK
        assert len(playwright.calls) == 1
        assert len(scrapling.calls) == 0  # never reached

    @pytest.mark.asyncio
    async def test_chain_keeps_order_for_normal_url(self) -> None:
        scrapling = _MockProvider(name="scrapling", result=_ok_result())
        playwright = _MockProvider(name="playwright", result=_ok_result())

        chain = ContentScraperChain(
            [scrapling, playwright],
            js_heavy_hosts=("techradar.com",),
        )
        result = await chain.scrape_markdown("https://example.com/article")

        assert result.status == CallStatus.OK
        assert len(scrapling.calls) == 1
        assert len(playwright.calls) == 0  # not reached, scrapling succeeded
