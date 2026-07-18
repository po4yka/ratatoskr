"""Crawlee-based advanced fallback provider for difficult pages."""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from typing import Any

from app.adapters.content.scraper.runtime_tuning import tuned_provider_timeout
from app.adapters.content.scraper.target_safety import reject_unsafe_target_url
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.html_utils import html_to_text
from app.core.logging_utils import get_logger
from app.security.ssrf import is_url_safe_async

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 45
_DEFAULT_MAX_RETRIES = 2


class CrawleeProvider:
    """Hybrid Crawlee provider: BeautifulSoup stage, then Playwright stage."""

    def __init__(
        self,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        headless: bool = True,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        *,
        min_content_length: int = 400,
        profile: str = "balanced",
        js_heavy_hosts: tuple[str, ...] = (),
    ) -> None:
        self._timeout_sec = timeout_sec
        self._headless = headless
        self._max_retries = max_retries
        self._min_content_length = min_content_length
        self._profile = profile
        self._js_heavy_hosts = js_heavy_hosts

    @property
    def provider_name(self) -> str:
        return "crawlee"

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        started = time.perf_counter()
        stage_errors: list[str] = []
        timeout_sec = tuned_provider_timeout(
            base_timeout_sec=self._timeout_sec,
            profile=self._profile,
            provider="crawlee",
            url=url,
            js_heavy_hosts=self._js_heavy_hosts,
        )
        unsafe_result = await reject_unsafe_target_url(
            provider="crawlee",
            url=url,
            started=started,
            request_id=request_id,
        )
        if unsafe_result is not None:
            return unsafe_result

        # Stage 1: lightweight HTTP-first crawl.
        try:
            bs_html = await asyncio.wait_for(
                self._extract_with_beautifulsoup(url, timeout_sec=timeout_sec),
                timeout=timeout_sec + 5,
            )
        except TimeoutError:
            bs_html = None
            stage_errors.append(f"BeautifulSoup timeout after {round(timeout_sec, 2)}s")
        except Exception as exc:
            bs_html = None
            stage_errors.append(f"BeautifulSoup error: {exc}")

        if bs_html:
            ok_result = self._build_success_result(
                html=bs_html,
                url=url,
                latency_ms=int((time.perf_counter() - started) * 1000),
                stage="beautifulsoup",
                mobile=mobile,
            )
            if ok_result is not None:
                return ok_result
            stage_errors.append("BeautifulSoup stage produced insufficient content")
        else:
            stage_errors.append("BeautifulSoup stage produced no HTML")

        # Stage 2: browser rendering fallback.
        try:
            pw_html = await asyncio.wait_for(
                self._extract_with_playwright(url, mobile=mobile, timeout_sec=timeout_sec),
                timeout=timeout_sec + 10,
            )
        except TimeoutError:
            pw_html = None
            stage_errors.append(f"Playwright timeout after {round(timeout_sec, 2)}s")
        except Exception as exc:
            pw_html = None
            stage_errors.append(f"Playwright error: {exc}")

        if pw_html:
            ok_result = self._build_success_result(
                html=pw_html,
                url=url,
                latency_ms=int((time.perf_counter() - started) * 1000),
                stage="playwright",
                mobile=mobile,
            )
            if ok_result is not None:
                return ok_result
            stage_errors.append("Playwright stage produced insufficient content")
        else:
            stage_errors.append("Playwright stage produced no HTML")

        latency = int((time.perf_counter() - started) * 1000)
        logger.info(
            "crawlee_exhausted",
            extra={
                "url": url,
                "request_id": request_id,
                "latency_ms": latency,
                "errors": stage_errors,
            },
        )

        return FirecrawlResult(
            status=CallStatus.ERROR,
            error_text=f"Crawlee exhausted: {'; '.join(stage_errors)}",
            latency_ms=latency,
            source_url=url,
            endpoint="crawlee",
        )

    def _build_success_result(
        self,
        *,
        html: str,
        url: str,
        latency_ms: int,
        stage: str,
        mobile: bool,
    ) -> FirecrawlResult | None:
        if not html.strip():
            return None

        content_text = html_to_text(html)
        if len(content_text) < self._min_content_length:
            return None

        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=None,
            content_html=html,
            latency_ms=latency_ms,
            source_url=url,
            endpoint="crawlee",
            options_json={
                "provider": "crawlee",
                "stage": stage,
                "headless": self._headless,
                "mobile": mobile,
                "max_retries": self._max_retries,
            },
        )

    async def _extract_with_beautifulsoup(self, url: str, *, timeout_sec: float) -> str | None:
        try:
            from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext
        except ImportError as exc:
            msg = (
                "Crawlee BeautifulSoup crawler is required. "
                "Install with: pip install 'crawlee[beautifulsoup,playwright]'"
            )
            raise ImportError(msg) from exc

        extracted_html: str | None = None
        crawler = BeautifulSoupCrawler(
            max_request_retries=self._max_retries,
            request_handler_timeout=timedelta(seconds=timeout_sec),
            max_requests_per_crawl=1,
        )

        @crawler.router.default_handler
        async def request_handler(context: BeautifulSoupCrawlingContext) -> None:
            nonlocal extracted_html
            extracted_html = str(context.soup) if context.soup is not None else None

        await crawler.run([url])
        return extracted_html

    async def _extract_with_playwright(
        self,
        url: str,
        *,
        mobile: bool = True,
        timeout_sec: float,
    ) -> str | None:
        del mobile  # Crawlee controls browser context; provider keeps API symmetry.
        try:
            from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
        except ImportError as exc:
            msg = (
                "Crawlee Playwright crawler is required. "
                "Install with: pip install 'crawlee[beautifulsoup,playwright]'"
            )
            raise ImportError(msg) from exc

        # Use browserforge-backed fingerprint generation (Crawlee's default) and
        # augment with a freshly generated fingerprint's HTTP headers so each
        # request gets a rotated, consistent UA + Sec-CH-UA set.
        try:
            import importlib as _il

            _bf_fp = _il.import_module("browserforge.fingerprints")
            _fp = _bf_fp.FingerprintGenerator(
                browser=["chrome"], device=["desktop"], os=["windows"]
            ).generate()
            _extra_headers = {
                k: v for k, v in (_fp.headers or {}).items() if k.lower() != "user-agent"
            }
            _new_context_opts: dict[str, Any] = {
                "user_agent": _fp.navigator.userAgent,
                "extra_http_headers": _extra_headers,
            }
        except Exception:
            _new_context_opts = {}

        extracted_html: str | None = None
        crawler = PlaywrightCrawler(
            headless=self._headless,
            max_request_retries=self._max_retries,
            request_handler_timeout=timedelta(seconds=timeout_sec),
            max_requests_per_crawl=1,
            browser_new_context_options=_new_context_opts or None,
        )

        # Re-validate every browser-initiated request (redirects + subresources
        # such as images/scripts/XHR) against the SSRF blocklist, not just the
        # initial navigation URL. Mirrors PlaywrightProvider._block_ssrf_route so
        # a compromised or attacker-controlled page cannot pivot the browser to
        # an internal address (cloud metadata, other compose services, loopback).
        crawler.pre_navigation_hook(self._install_ssrf_guard)

        @crawler.router.default_handler
        async def request_handler(context: PlaywrightCrawlingContext) -> None:
            nonlocal extracted_html
            extracted_html = await context.page.content()

        await crawler.run([url])
        return extracted_html

    async def _install_ssrf_guard(self, context: Any) -> None:
        """Install a per-request SSRF filter on the page before navigation."""
        await context.page.route("**/*", self._ssrf_guard_route)

    async def _ssrf_guard_route(self, route: Any) -> None:
        """Abort browser requests whose (post-redirect) target is unsafe.

        DNS-rebinding caveat is identical to PlaywrightProvider: Chromium does
        its own DNS resolution at connect time, so a TTL=0 host that serves a
        public IP to our resolver and a private IP to the browser is not caught
        here. Close that residual gap at the network/egress layer.
        """
        req_url = str(route.request.url)
        safe, reason = await is_url_safe_async(req_url)
        if not safe:
            logger.warning(
                "crawlee_ssrf_blocked",
                extra={"url": req_url, "reason": reason},
            )
            await route.abort("accessdenied")
            return
        await route.continue_()

    async def aclose(self) -> None:
        pass
