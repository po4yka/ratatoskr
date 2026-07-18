"""Tests for CrawleeProvider's per-request SSRF guard on the Playwright stage.

The Playwright stage renders arbitrary user-submitted URLs in a real browser, so
every browser-initiated request (redirects + subresources) must be re-validated
against the SSRF blocklist -- not just the initial navigation URL. These tests
verify the ``page.route`` guard aborts unsafe targets and allows safe ones, and
that the guard is installed via a pre-navigation hook before navigation.

Crawlee is an optional extra and is not installed in the unit-test environment,
so the wiring test injects a fake ``crawlee.crawlers`` module the same way
``test_playwright_provider`` mocks ``playwright.sync_api``.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.content.scraper.crawlee_provider import CrawleeProvider

pytestmark = pytest.mark.no_network


def _make_route(url: str) -> MagicMock:
    route = MagicMock()
    route.request.url = url
    route.abort = AsyncMock()
    route.continue_ = AsyncMock()
    return route


@pytest.mark.asyncio
async def test_ssrf_guard_aborts_unsafe_url(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = CrawleeProvider()
    monkeypatch.setattr(
        "app.adapters.content.scraper.crawlee_provider.is_url_safe_async",
        AsyncMock(return_value=(False, "Blocked private address")),
    )
    route = _make_route("http://169.254.169.254/latest/meta-data/")

    await provider._ssrf_guard_route(route)

    route.abort.assert_awaited_once()
    route.continue_.assert_not_called()


@pytest.mark.asyncio
async def test_ssrf_guard_allows_safe_url(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = CrawleeProvider()
    monkeypatch.setattr(
        "app.adapters.content.scraper.crawlee_provider.is_url_safe_async",
        AsyncMock(return_value=(True, None)),
    )
    route = _make_route("https://example.com/article")

    await provider._ssrf_guard_route(route)

    route.continue_.assert_awaited_once()
    route.abort.assert_not_called()


@pytest.mark.asyncio
async def test_install_ssrf_guard_registers_page_route() -> None:
    provider = CrawleeProvider()
    context = MagicMock()
    context.page.route = AsyncMock()

    await provider._install_ssrf_guard(context)

    context.page.route.assert_awaited_once_with("**/*", provider._ssrf_guard_route)


class _FakeRouter:
    def __init__(self) -> None:
        self.handler: Any = None

    def default_handler(self, func: Any) -> Any:
        self.handler = func
        return func


class _FakePlaywrightCrawler:
    """Minimal stand-in that records pre-navigation hooks and runs them."""

    last_instance: _FakePlaywrightCrawler | None = None

    def __init__(self, **_kwargs: Any) -> None:
        self.router = _FakeRouter()
        self.pre_nav_hooks: list[Any] = []
        self.prenav_context = MagicMock()
        self.prenav_context.page.route = AsyncMock()
        _FakePlaywrightCrawler.last_instance = self

    def pre_navigation_hook(self, hook: Any) -> None:
        self.pre_nav_hooks.append(hook)

    async def run(self, _urls: list[str]) -> None:
        # Simulate Crawlee firing pre-navigation hooks before navigation, then
        # the request handler producing page content.
        for hook in self.pre_nav_hooks:
            await hook(self.prenav_context)
        ctx = MagicMock()
        ctx.page.content = AsyncMock(return_value="<html><body>ok</body></html>")
        if self.router.handler is not None:
            await self.router.handler(ctx)


@pytest.mark.asyncio
async def test_playwright_stage_installs_ssrf_guard_before_navigation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_crawlers = ModuleType("crawlee.crawlers")
    fake_crawlers.PlaywrightCrawler = _FakePlaywrightCrawler  # type: ignore[attr-defined]
    fake_crawlers.PlaywrightCrawlingContext = MagicMock  # type: ignore[attr-defined]
    fake_pkg = ModuleType("crawlee")
    fake_pkg.crawlers = fake_crawlers  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "crawlee", fake_pkg)
    monkeypatch.setitem(sys.modules, "crawlee.crawlers", fake_crawlers)

    provider = CrawleeProvider()
    html = await provider._extract_with_playwright("https://example.com", timeout_sec=5.0)

    assert html == "<html><body>ok</body></html>"
    crawler = _FakePlaywrightCrawler.last_instance
    assert crawler is not None
    # The guard installer was registered as a pre-navigation hook...
    assert provider._install_ssrf_guard in crawler.pre_nav_hooks
    # ...and firing it installed the per-request route filter on the page.
    crawler.prenav_context.page.route.assert_awaited_once_with("**/*", provider._ssrf_guard_route)
