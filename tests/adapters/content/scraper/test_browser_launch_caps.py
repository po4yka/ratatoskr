"""Every provider that launches a browser in-container must cap how many it starts.

The chain races the whole browser tier for one URL, so an uncapped provider does
not just add one browser -- it adds one per in-flight request, alongside the
capped ones. CrawleeProvider was written without a cap while its two neighbours
had one, which is the shape that put a memory-limited container in reach of the
OOM killer.

The provider list is derived from the chain's own tier membership rather than
listed here, so a new browser rung fails this instead of quietly joining the race
uncapped.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.adapters.content.scraper.browser_concurrency import launch_semaphore
from app.adapters.content.scraper.chain import _BROWSER_TIER_PROVIDERS

ROOT = Path(__file__).resolve().parents[4]
SCRAPER_DIR = ROOT / "app" / "adapters" / "content" / "scraper"

# Providers that drive a browser running elsewhere rather than starting one in
# this container: nothing local to cap.
_SIDECAR_PROVIDERS = {"cloakbrowser", "scrapegraph_ai"}

_IN_CONTAINER = sorted(_BROWSER_TIER_PROVIDERS - _SIDECAR_PROVIDERS)


def test_the_derivation_found_the_providers() -> None:
    """A derivation that matches nothing would make the assertions below vacuous."""
    assert "crawlee" in _IN_CONTAINER
    assert "playwright" in _IN_CONTAINER


@pytest.mark.parametrize("provider", _IN_CONTAINER)
def test_in_container_browser_provider_holds_a_cap(provider: str) -> None:
    module = SCRAPER_DIR / f"{provider}_provider.py"
    assert module.is_file(), f"no module for browser-tier provider {provider!r}"
    source = module.read_text(encoding="utf-8")

    assert "semaphore" in source.lower(), (
        f"{provider} launches a browser in-container but caps nothing, so a burst "
        "of browser-tier fallbacks spawns one browser per request"
    )


async def test_playwright_and_crawlee_share_one_cap() -> None:
    """The two in-container rungs race for the same URL, so their cap must be shared.

    With a private cap each, a single request could start two browsers per
    provider -- four in one container, which is worse than the status quo in
    exactly the case the cap exists for.
    """
    from app.adapters.content.scraper import crawlee_provider, playwright_provider

    assert playwright_provider._playwright_launch_semaphore() is (
        crawlee_provider.chromium_launch_semaphore()
    )


async def test_the_cap_actually_bounds_concurrency() -> None:
    semaphore = launch_semaphore("test-cap", env_var="RATATOSKR_TEST_CAP_UNSET", default=2)
    live = 0
    peak = 0
    release = asyncio.Event()

    async def _launch() -> None:
        nonlocal live, peak
        async with semaphore:
            live += 1
            peak = max(peak, live)
            await release.wait()
            live -= 1

    tasks = [asyncio.create_task(_launch()) for _ in range(6)]
    await asyncio.sleep(0.05)
    observed = peak
    release.set()
    await asyncio.gather(*tasks)

    assert observed == 2, f"{observed} concurrent launches against a cap of 2"


async def test_the_cap_is_reused_per_name_and_loop() -> None:
    """Rebuilding the semaphore per call would make the cap meaningless."""
    first = launch_semaphore("reuse-check", env_var="RATATOSKR_TEST_CAP_UNSET", default=3)
    second = launch_semaphore("reuse-check", env_var="RATATOSKR_TEST_CAP_UNSET", default=3)

    assert first is second


async def test_separate_names_get_separate_caps() -> None:
    assert launch_semaphore("a", env_var="RATATOSKR_TEST_CAP_UNSET", default=1) is not (
        launch_semaphore("b", env_var="RATATOSKR_TEST_CAP_UNSET", default=1)
    )


async def test_the_env_var_overrides_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RATATOSKR_TEST_CAP_SET", "5")

    semaphore = launch_semaphore("env-override", env_var="RATATOSKR_TEST_CAP_SET", default=2)

    assert semaphore._value == 5


async def test_a_malformed_env_var_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo must not remove the cap entirely."""
    monkeypatch.setenv("RATATOSKR_TEST_CAP_BAD", "not-a-number")

    semaphore = launch_semaphore("bad-env", env_var="RATATOSKR_TEST_CAP_BAD", default=2)

    assert semaphore._value == 2
