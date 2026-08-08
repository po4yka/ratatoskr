"""One scrape must not hold its caller for the sum of every tier's timeout.

Per-provider timeouts bound each attempt, but tiers run in sequence, so a URL
that fails slowly at every rung added them up with nothing capping the total.
"""

from __future__ import annotations

import asyncio

import pytest

from app.adapters.content.scraper.chain import ContentScraperChain
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus


class _SlowProvider:
    def __init__(self, name: str, delay: float) -> None:
        self._name = name
        self._delay = delay
        self.started = 0

    @property
    def provider_name(self) -> str:
        return self._name

    async def scrape_markdown(self, url: str, **_kwargs: object) -> FirecrawlResult:
        self.started += 1
        await asyncio.sleep(self._delay)
        return FirecrawlResult(
            status=CallStatus.ERROR, error_text="slow", source_url=url, endpoint=self._name
        )


class _FastWinner:
    @property
    def provider_name(self) -> str:
        return "direct_html"

    async def scrape_markdown(self, url: str, **_kwargs: object) -> FirecrawlResult:
        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=(
                "This is a real article with substantial prose so the chain's "
                "quality filters accept it as content rather than treating it as "
                "an error page or a low-value stub. It runs on for several "
                "sentences with ordinary words and punctuation. "
            )
            * 4,
            source_url=url,
            endpoint="direct_html",
        )


@pytest.fixture(autouse=True)
def _allow_target(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _safe(_url: str, **_kwargs: object) -> tuple[bool, None]:
        return True, None

    monkeypatch.setattr("app.adapters.content.scraper.chain.is_url_safe_async", _safe)


async def test_the_budget_cuts_off_a_run_that_would_outlast_it() -> None:
    chain = ContentScraperChain(
        [_SlowProvider("scrapling", 5.0)],
        min_content_length=1,
        race_enabled=False,
        total_timeout_sec=0.2,
    )

    started = asyncio.get_running_loop().time()
    result = await chain.scrape_markdown("https://example.com/article")
    elapsed = asyncio.get_running_loop().time() - started

    assert result.status == CallStatus.ERROR
    assert "budget" in (result.error_text or "").lower(), (
        f"the timeout must be reported as a budget cut-off, got: {result.error_text}"
    )
    assert elapsed < 2.0, f"the chain ran {elapsed:.1f}s against a 0.2s budget"


async def test_a_run_inside_the_budget_is_untouched() -> None:
    """The cap must only ever cut off a run that was going to fail anyway."""
    chain = ContentScraperChain(
        [_FastWinner()],
        min_content_length=1,
        race_enabled=False,
        total_timeout_sec=30.0,
    )

    result = await chain.scrape_markdown("https://example.com/article")

    assert result.status == CallStatus.OK
    assert result.content_markdown


async def test_zero_disables_the_budget() -> None:
    """Directly-constructed chains keep their previous unbounded behaviour."""
    chain = ContentScraperChain(
        [_FastWinner()],
        min_content_length=1,
        race_enabled=False,
        total_timeout_sec=0.0,
    )

    result = await chain.scrape_markdown("https://example.com/article")

    assert result.status == CallStatus.OK


async def test_the_timed_out_run_still_carries_its_attempt_log() -> None:
    """Telemetry is how a budget cut-off gets diagnosed; it must survive."""
    chain = ContentScraperChain(
        [_SlowProvider("scrapling", 5.0)],
        min_content_length=1,
        race_enabled=False,
        total_timeout_sec=0.2,
    )

    result = await chain.scrape_markdown("https://example.com/article")

    assert "_chain_attempt_log" in (result.options_json or {})
