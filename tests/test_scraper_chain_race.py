"""Tests for ContentScraperChain tiered-race mode (race_enabled=True).

The race path replaces the serial fallback for the ``free`` (scrapling,
defuddle, direct_html, crawl4ai) and ``browser`` (playwright, crawlee,
cloakbrowser, scrapegraph_ai) tiers. Firecrawl stays serial in its own
``paid`` tier so we never burn a billed Firecrawl call when a free
provider would have won. Tier ordering: ``pdf`` → ``free`` → ``paid`` →
``browser`` → ``other``.
"""

from __future__ import annotations

import asyncio

import pytest

from app.adapters.content.scraper.chain import ContentScraperChain
from tests.helpers.scraper_helpers import _MockProvider, _error_result, _ok_result


class _SlowProvider:
    """Sleeps before returning its result; records when cancelled.

    Lets us verify that a fast provider wins the race even when a slower
    sibling is still mid-flight and that the slow sibling is cancelled.
    """

    def __init__(self, *, name: str, result, delay_sec: float, cancelled_flag=None) -> None:
        self._name = name
        self._result = result
        self._delay_sec = delay_sec
        self._cancelled_flag = cancelled_flag
        self.calls: list[dict] = []

    @property
    def provider_name(self) -> str:
        return self._name

    async def scrape_markdown(self, url, *, mobile=True, request_id=None):
        self.calls.append({"url": url, "mobile": mobile, "request_id": request_id})
        try:
            await asyncio.sleep(self._delay_sec)
            return self._result
        except asyncio.CancelledError:
            if self._cancelled_flag is not None:
                self._cancelled_flag.append(self._name)
            raise

    async def aclose(self) -> None:  # pragma: no cover — protocol compliance
        pass


class TestFreeTierRace:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_fast_free_provider_wins_race_and_cancels_siblings(self):
        cancelled: list[str] = []
        scrapling = _MockProvider(name="scrapling", result=_ok_result(markdown="# Scrapling fast"))
        defuddle = _SlowProvider(
            name="defuddle",
            result=_ok_result(markdown="# Defuddle slow"),
            delay_sec=0.5,
            cancelled_flag=cancelled,
        )

        chain = ContentScraperChain([scrapling, defuddle], race_enabled=True)
        result = await chain.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown == "# Scrapling fast"
        assert cancelled == ["defuddle"]

    @pytest.mark.asyncio(loop_scope="function")
    async def test_free_winner_short_circuits_browser_tier(self):
        """Free-tier winner means browser providers never run.

        This is the core speedup behind Tier 1: when scrapling or
        direct_html succeeds, the chain returns immediately and the
        expensive browser providers never get a chance.
        """
        scrapling = _MockProvider(name="scrapling", result=_ok_result(markdown="# Scrapling"))
        playwright = _MockProvider(name="playwright", result=_ok_result(markdown="# Heavy"))

        chain = ContentScraperChain([scrapling, playwright], race_enabled=True)
        result = await chain.scrape_markdown("https://example.com")

        assert result.content_markdown == "# Scrapling"
        assert len(scrapling.calls) == 1
        assert len(playwright.calls) == 0

    @pytest.mark.asyncio(loop_scope="function")
    async def test_all_free_fail_then_paid_runs(self):
        """When every free provider errors, the paid tier (Firecrawl) gets a turn."""
        scrapling = _MockProvider(name="scrapling", result=_error_result(error="scrapling failed"))
        defuddle = _MockProvider(name="defuddle", result=_error_result(error="defuddle failed"))
        firecrawl = _MockProvider(name="firecrawl", result=_ok_result(markdown="# Firecrawl"))

        chain = ContentScraperChain([scrapling, defuddle, firecrawl], race_enabled=True)
        result = await chain.scrape_markdown("https://example.com")

        assert result.content_markdown == "# Firecrawl"
        assert len(scrapling.calls) == 1
        assert len(defuddle.calls) == 1
        assert len(firecrawl.calls) == 1


class TestPaidTierSerial:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_paid_tier_skipped_when_free_wins(self):
        """We never spend a Firecrawl call when a free provider succeeds."""
        scrapling = _MockProvider(name="scrapling", result=_ok_result(markdown="# Scrapling"))
        firecrawl = _MockProvider(name="firecrawl", result=_ok_result(markdown="# Firecrawl"))

        chain = ContentScraperChain([scrapling, firecrawl], race_enabled=True)
        result = await chain.scrape_markdown("https://example.com")

        assert result.content_markdown == "# Scrapling"
        assert len(firecrawl.calls) == 0


class TestBrowserTierRace:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_browser_tier_races_when_reached(self):
        scrapling = _MockProvider(name="scrapling", result=_error_result(error="scrapling failed"))
        firecrawl = _MockProvider(name="firecrawl", result=_error_result(error="firecrawl failed"))
        cancelled: list[str] = []
        playwright = _MockProvider(name="playwright", result=_ok_result(markdown="# Playwright"))
        crawlee = _SlowProvider(
            name="crawlee",
            result=_ok_result(markdown="# Crawlee"),
            delay_sec=0.5,
            cancelled_flag=cancelled,
        )

        chain = ContentScraperChain(
            [scrapling, firecrawl, playwright, crawlee], race_enabled=True
        )
        result = await chain.scrape_markdown("https://example.com")

        assert result.content_markdown == "# Playwright"
        assert cancelled == ["crawlee"]


class TestRaceDisabledRoundTrip:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_race_disabled_preserves_strict_order(self):
        """``race_enabled=False`` is the legacy ordered-fallback path."""
        scrapling = _MockProvider(name="scrapling", result=_ok_result(markdown="# Scrapling"))
        direct_html = _MockProvider(name="direct_html", result=_ok_result(markdown="# Direct"))

        chain = ContentScraperChain([scrapling, direct_html], race_enabled=False)
        result = await chain.scrape_markdown("https://example.com")

        # Serial mode walks providers in input order so scrapling always wins.
        assert result.content_markdown == "# Scrapling"
        assert len(direct_html.calls) == 0
