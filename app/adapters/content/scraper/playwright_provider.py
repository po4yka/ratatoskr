"""Playwright-based fallback provider for JS-rendered pages.

The ``slim`` constructor flag is forwarded to ``FingerprintGenerator(slim=slim,
...)`` (browserforge) to generate lighter fingerprints with fewer JS property
overrides.  This reduces per-launch memory and init cost and is a sensible
default for sites that are not strongly bot-protected.  Set ``slim=False``
(the default) to keep the full fingerprint for maximum stealth on hard sites.
"""

from __future__ import annotations

import asyncio
import os
import time
import weakref
from typing import cast

from app.adapters.content.scraper.runtime_tuning import is_js_heavy_url, tuned_provider_timeout
from app.adapters.content.scraper.target_safety import reject_unsafe_target_url
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.html_utils import html_to_text
from app.core.logging_utils import get_logger
from app.security.ssrf import is_url_safe

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 30


def _playwright_max_concurrency() -> int:
    """Max number of concurrent Chromium browser launches for PlaywrightProvider."""
    try:
        return max(1, int(os.getenv("PLAYWRIGHT_MAX_CONCURRENT_BROWSERS", "2")))
    except ValueError:
        return 2


# A Playwright fetch launches a full browser process; without a cap, a burst of
# upstream failures could spawn one browser per request and exhaust file
# descriptors, RAM, and thread-pool workers. The semaphore is keyed per event
# loop so it binds lazily (and stays correct across test loops).
_playwright_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _playwright_launch_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _playwright_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_playwright_max_concurrency())
        _playwright_semaphores[loop] = sem
    return sem


class PlaywrightProvider:
    """Browser-rendered fallback for pages requiring JavaScript execution."""

    def __init__(
        self,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        headless: bool = True,
        *,
        min_text_length: int = 400,
        profile: str = "balanced",
        js_heavy_hosts: tuple[str, ...] = (),
        slim: bool = False,
    ) -> None:
        self._timeout_sec = timeout_sec
        self._headless = headless
        self._min_text_length = min_text_length
        self._profile = profile
        self._js_heavy_hosts = js_heavy_hosts
        self._slim = slim

    @property
    def provider_name(self) -> str:
        return "playwright"

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        started = time.perf_counter()
        timeout_sec = tuned_provider_timeout(
            base_timeout_sec=self._timeout_sec,
            profile=self._profile,
            provider="playwright",
            url=url,
            js_heavy_hosts=self._js_heavy_hosts,
        )
        unsafe_result = await reject_unsafe_target_url(
            provider="playwright",
            url=url,
            started=started,
            request_id=request_id,
        )
        if unsafe_result is not None:
            return unsafe_result

        try:
            html = await asyncio.wait_for(
                self._render_html(url, mobile=mobile, timeout_sec=timeout_sec),
                timeout=timeout_sec + 5,
            )
        except TimeoutError:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "playwright_timeout",
                extra={"url": url, "timeout_sec": round(timeout_sec, 2), "request_id": request_id},
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Playwright timeout after {round(timeout_sec, 2)}s",
                latency_ms=latency,
                source_url=url,
                endpoint="playwright",
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "playwright_error",
                extra={
                    "url": url,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Playwright error: {exc}",
                latency_ms=latency,
                source_url=url,
                endpoint="playwright",
            )

        latency = int((time.perf_counter() - started) * 1000)
        if not html:
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="Playwright: no usable content",
                latency_ms=latency,
                source_url=url,
                endpoint="playwright",
            )

        content_text = html_to_text(html)
        if len(content_text) < self._min_text_length:
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Playwright: content too short ({len(content_text)} chars)",
                content_html=html,
                latency_ms=latency,
                source_url=url,
                endpoint="playwright",
            )

        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=None,
            content_html=html,
            latency_ms=latency,
            source_url=url,
            endpoint="playwright",
            options_json={
                "provider": "playwright",
                "headless": self._headless,
                "mobile": mobile,
            },
        )

    async def _render_html(
        self, url: str, *, mobile: bool = True, timeout_sec: float | None = None
    ) -> str | None:
        async with _playwright_launch_semaphore():
            return await asyncio.to_thread(
                self._render_html_sync,
                url,
                mobile=mobile,
                timeout_sec=timeout_sec,
            )

    def _render_html_sync(
        self,
        url: str,
        *,
        mobile: bool = True,
        timeout_sec: float | None = None,
    ) -> str | None:
        try:
            from playwright.sync_api import (
                Error as PlaywrightError,
                TimeoutError as PlaywrightTimeoutError,
                sync_playwright,
            )
        except ImportError as exc:
            msg = (
                "Playwright is required for scraper fallback. "
                "Install with: pip install 'playwright>=1.40' && playwright install chromium"
            )
            raise ImportError(msg) from exc

        # Lazy imports so environments without browserforge still load the module.
        try:
            import importlib

            _bf_fp_mod = importlib.import_module("browserforge.fingerprints")
            _bf_inj_mod = importlib.import_module("browserforge.injectors.playwright")
            _fp_generator_cls = _bf_fp_mod.FingerprintGenerator
            _new_context_fn = _bf_inj_mod.NewContext
            _bf_available = True
        except Exception:
            _bf_available = False

        effective_timeout_sec = timeout_sec if timeout_sec is not None else self._timeout_sec
        timeout_ms = max(1_000, int(effective_timeout_sec * 1000))
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self._headless,
                args=["--disable-blink-features=AutomationControlled"],
            )
            if _bf_available:
                if mobile:
                    fingerprint = _fp_generator_cls(
                        browser=["chrome"],
                        device=["mobile"],
                        os=["android"],
                        slim=self._slim,
                    ).generate()
                else:
                    fingerprint = _fp_generator_cls(
                        browser=["chrome"],
                        device=["desktop"],
                        os=["windows"],
                        slim=self._slim,
                    ).generate()
                context = _new_context_fn(browser, fingerprint=fingerprint)
            elif mobile:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Linux; Android 11; Pixel 5) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Mobile Safari/537.36"
                    ),
                    viewport={"width": 390, "height": 844},
                    is_mobile=True,
                    has_touch=True,
                )
            else:
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1366, "height": 768},
                )
            page = context.new_page()

            def _block_ssrf_route(route: object) -> None:
                # Best-effort URL-level SSRF filter for browser-initiated requests.
                # Does not close the DNS-rebinding window (browser DNS is opaque).
                req_url: str = route.request.url  # type: ignore[attr-defined]
                safe, reason = is_url_safe(req_url)
                if not safe:
                    logger.warning(
                        "playwright_ssrf_blocked",
                        extra={"url": req_url, "reason": reason},
                    )
                    route.abort("accessdenied")  # type: ignore[attr-defined]
                    return
                route.continue_()  # type: ignore[attr-defined]

            page.route("**/*", _block_ssrf_route)

            try:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except (PlaywrightTimeoutError, PlaywrightError):
                    logger.debug(
                        "playwright_goto_failed_partial_capture_mode",
                        extra={"url": url},
                        exc_info=True,
                    )

                # For JS-heavy hosts, wait for article content to hydrate
                # before scrolling. Best-effort -- timeout is non-fatal.
                if self._js_heavy_hosts and is_js_heavy_url(url, self._js_heavy_hosts):
                    try:
                        page.wait_for_selector(
                            "article p, main p, [role='article'] p, .article-body p",
                            timeout=8_000,
                        )
                    except (PlaywrightTimeoutError, PlaywrightError):
                        logger.debug(
                            "playwright_article_selector_wait_timeout",
                            extra={"url": url},
                        )

                # Try to trigger lazy-loading content without over-delaying fallback chain.
                for _ in range(4):
                    page.evaluate("window.scrollBy(0, window.innerHeight)")
                    page.wait_for_timeout(250)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(200)

                try:
                    page.wait_for_load_state("networkidle", timeout=min(5_000, timeout_ms))
                except (PlaywrightTimeoutError, PlaywrightError):
                    logger.debug(
                        "playwright_networkidle_wait_failed",
                        extra={"url": url},
                        exc_info=True,
                    )

                return cast("str | None", page.content())
            finally:
                page.close()
                context.close()
                browser.close()

    async def aclose(self) -> None:
        pass
