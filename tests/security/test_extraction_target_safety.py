from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.adapters.content.scraper.crawl4ai_provider import Crawl4AIProvider
from app.adapters.content.scraper.crawlee_provider import CrawleeProvider
from app.adapters.content.scraper.defuddle_provider import DefuddleProvider
from app.adapters.content.scraper.firecrawl_provider import FirecrawlProvider
from app.adapters.content.scraper.playwright_provider import PlaywrightProvider
from app.adapters.content.scraper.scrapegraph_provider import ScrapeGraphAIProvider
from app.adapters.content.scraper.scrapling_provider import ScraplingProvider
from app.adapters.content.scraper.webwright_provider import WebwrightProvider
from app.core.call_status import CallStatus

_BLOCKED = (False, "Hostname resolves to blocked address: 169.254.169.254")
_UNSAFE_URL = "http://169.254.169.254/latest/meta-data/"


@pytest.fixture(autouse=True)
def _block_target_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.adapters.content.scraper.target_safety.is_url_safe_async",
        AsyncMock(return_value=_BLOCKED),
    )


@pytest.mark.asyncio
async def test_scrapling_blocks_unsafe_target_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock()
    provider = ScraplingProvider()
    monkeypatch.setattr(provider, "_fetch", fetch)

    result = await provider.scrape_markdown(_UNSAFE_URL)

    assert result.status == CallStatus.ERROR
    assert "SSRF blocked URL" in (result.error_text or "")
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_firecrawl_blocks_unsafe_target_before_client() -> None:
    client = AsyncMock()
    provider = FirecrawlProvider(client)

    result = await provider.scrape_markdown(_UNSAFE_URL)

    assert result.status == CallStatus.ERROR
    assert "SSRF blocked URL" in (result.error_text or "")
    client.scrape_markdown.assert_not_awaited()


@pytest.mark.asyncio
async def test_crawl4ai_blocks_unsafe_target_before_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = Crawl4AIProvider("http://crawl4ai:11235")
    post = AsyncMock()
    monkeypatch.setattr(provider, "_get_client", lambda: type("Client", (), {"post": post})())

    result = await provider.scrape_markdown(_UNSAFE_URL)

    assert result.status == CallStatus.ERROR
    assert "SSRF blocked URL" in (result.error_text or "")
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_defuddle_blocks_unsafe_target_before_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch = AsyncMock()
    provider = DefuddleProvider()
    monkeypatch.setattr(provider, "_fetch_raw", fetch)

    result = await provider.scrape_markdown(_UNSAFE_URL)

    assert result.status == CallStatus.ERROR
    assert "SSRF blocked URL" in (result.error_text or "")
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_playwright_blocks_unsafe_target_before_browser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render = AsyncMock()
    provider = PlaywrightProvider()
    monkeypatch.setattr(provider, "_render_html", render)

    result = await provider.scrape_markdown(_UNSAFE_URL)

    assert result.status == CallStatus.ERROR
    assert "SSRF blocked URL" in (result.error_text or "")
    render.assert_not_awaited()


@pytest.mark.asyncio
async def test_crawlee_blocks_unsafe_target_before_crawlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bs_fetch = AsyncMock()
    pw_fetch = AsyncMock()
    provider = CrawleeProvider()
    monkeypatch.setattr(provider, "_extract_with_beautifulsoup", bs_fetch)
    monkeypatch.setattr(provider, "_extract_with_playwright", pw_fetch)

    result = await provider.scrape_markdown(_UNSAFE_URL)

    assert result.status == CallStatus.ERROR
    assert "SSRF blocked URL" in (result.error_text or "")
    bs_fetch.assert_not_awaited()
    pw_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_webwright_blocks_unsafe_target_before_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post_scrape = AsyncMock()
    provider = WebwrightProvider(host_allowlist=("*",))
    monkeypatch.setattr(provider, "_post_scrape", post_scrape)

    result = await provider.scrape_markdown(_UNSAFE_URL)

    assert result.status == CallStatus.ERROR
    assert "SSRF blocked URL" in (result.error_text or "")
    post_scrape.assert_not_awaited()


@pytest.mark.asyncio
async def test_scrapegraph_blocks_unsafe_target_before_graph_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_module = AsyncMock()
    provider = ScrapeGraphAIProvider("key", "model")
    monkeypatch.setattr(
        "app.adapters.content.scraper.scrapegraph_provider.importlib.import_module", import_module
    )

    result = await provider.scrape_markdown(_UNSAFE_URL)

    assert result.status == CallStatus.ERROR
    assert "SSRF blocked URL" in (result.error_text or "")
    import_module.assert_not_called()
