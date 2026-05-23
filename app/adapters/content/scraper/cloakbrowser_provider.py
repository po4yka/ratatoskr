"""CloakBrowser CDP-sidecar content extraction provider.

Connects to a CloakBrowser ``cloakserve`` instance over the Chrome DevTools
Protocol (default ``http://cloakbrowser:9222``). The sidecar runs the upstream
stealth Chromium build with C++ source-level fingerprint patches; we just
drive it through the standard Playwright API.

See ``ops/docker/docker-compose.yml`` for the ``cloakbrowser`` service that
this provider expects under the ``with-scrapers`` profile.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from app.adapters.content.scraper.runtime_tuning import tuned_provider_timeout
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.html_utils import html_to_text
from app.core.logging_utils import get_logger
from app.security.ssrf import is_url_safe

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 60

_MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Mobile Safari/537.36"
)
_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


class CloakBrowserProvider:
    """Stealth-browser provider backed by a CloakBrowser cloakserve sidecar."""

    def __init__(
        self,
        endpoint_url: str,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        *,
        min_text_length: int = 400,
        profile: str = "balanced",
        js_heavy_hosts: tuple[str, ...] = (),
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._min_text_length = min_text_length
        self._profile = profile
        self._js_heavy_hosts = js_heavy_hosts
        self._audit = audit

    @property
    def provider_name(self) -> str:
        return "cloakbrowser"

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        started = time.perf_counter()

        safe, reason = is_url_safe(url)
        if not safe:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "cloakbrowser_ssrf_blocked",
                extra={"url": url, "reason": reason, "request_id": request_id},
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"CloakBrowser SSRF blocked: {reason}",
                latency_ms=latency,
                source_url=url,
                endpoint="cloakbrowser",
            )

        effective_timeout = tuned_provider_timeout(
            base_timeout_sec=self._timeout_sec,
            profile=self._profile,
            provider="cloakbrowser",
            url=url,
            js_heavy_hosts=self._js_heavy_hosts,
        )
        timeout_ms = max(1_000, int(effective_timeout * 1000))

        try:
            html, http_status = await self._render(
                url, mobile=mobile, timeout_ms=timeout_ms
            )
        except TimeoutError:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "cloakbrowser_timeout",
                extra={
                    "url": url,
                    "timeout_sec": round(effective_timeout, 2),
                    "request_id": request_id,
                },
            )
            if self._audit:
                self._audit(
                    "ERROR",
                    "cloakbrowser_failure",
                    {
                        "url": url,
                        "error": "timeout",
                        "timeout_sec": round(effective_timeout, 2),
                        "request_id": request_id,
                    },
                )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"CloakBrowser timeout after {round(effective_timeout, 2)}s",
                latency_ms=latency,
                source_url=url,
                endpoint="cloakbrowser",
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "cloakbrowser_error",
                extra={
                    "url": url,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "request_id": request_id,
                },
            )
            if self._audit:
                self._audit(
                    "ERROR",
                    "cloakbrowser_failure",
                    {
                        "url": url,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "request_id": request_id,
                    },
                )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"CloakBrowser error: {exc}",
                latency_ms=latency,
                source_url=url,
                endpoint="cloakbrowser",
            )

        latency = int((time.perf_counter() - started) * 1000)
        if not html:
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="CloakBrowser: no usable content",
                latency_ms=latency,
                source_url=url,
                endpoint="cloakbrowser",
            )

        content_text = html_to_text(html)
        if len(content_text) < self._min_text_length:
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"CloakBrowser: content too short ({len(content_text)} chars)",
                content_html=html,
                http_status=http_status,
                latency_ms=latency,
                source_url=url,
                endpoint="cloakbrowser",
            )

        if self._audit:
            self._audit(
                "INFO",
                "cloakbrowser_request",
                {
                    "url": url,
                    "latency_ms": latency,
                    "content_len": len(content_text),
                    "request_id": request_id,
                },
            )

        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=http_status or 200,
            content_markdown=None,
            content_html=html,
            latency_ms=latency,
            source_url=url,
            endpoint="cloakbrowser",
            options_json={
                "provider": "cloakbrowser",
                "endpoint_url": self._endpoint_url,
                "mobile": mobile,
            },
        )

    async def _render(
        self, url: str, *, mobile: bool, timeout_ms: int
    ) -> tuple[str | None, int | None]:
        """Connect to cloakserve over CDP, render the page, return (html, status)."""
        try:
            from playwright.async_api import (
                Error as PlaywrightError,
                TimeoutError as PlaywrightTimeoutError,
                async_playwright,
            )
        except ImportError as exc:
            msg = (
                "Playwright is required for the CloakBrowser provider. "
                "Install with: pip install 'playwright>=1.40'"
            )
            raise ImportError(msg) from exc

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(self._endpoint_url)
            try:
                context = await browser.new_context(
                    user_agent=_MOBILE_UA if mobile else _DESKTOP_UA,
                    viewport=(
                        {"width": 390, "height": 844}
                        if mobile
                        else {"width": 1366, "height": 768}
                    ),
                    is_mobile=mobile,
                    has_touch=mobile,
                )
                try:
                    page = await context.new_page()

                    async def _block_ssrf_route(route: Any) -> None:
                        req_url: str = route.request.url
                        ok, why = is_url_safe(req_url)
                        if not ok:
                            logger.warning(
                                "cloakbrowser_ssrf_blocked_subrequest",
                                extra={"url": req_url, "reason": why},
                            )
                            await route.abort("accessdenied")
                            return
                        await route.continue_()

                    await page.route("**/*", _block_ssrf_route)

                    http_status: int | None = None
                    try:
                        response = await page.goto(
                            url, wait_until="domcontentloaded", timeout=timeout_ms
                        )
                        if response is not None:
                            http_status = response.status
                    except (PlaywrightTimeoutError, PlaywrightError):
                        logger.debug(
                            "cloakbrowser_goto_partial_capture",
                            extra={"url": url},
                            exc_info=True,
                        )

                    try:
                        await page.wait_for_load_state(
                            "networkidle", timeout=min(5_000, timeout_ms)
                        )
                    except (PlaywrightTimeoutError, PlaywrightError):
                        logger.debug(
                            "cloakbrowser_networkidle_wait_failed",
                            extra={"url": url},
                            exc_info=True,
                        )

                    html = await page.content()
                    return html, http_status
                finally:
                    await context.close()
            finally:
                # Disconnect from cloakserve without shutting it down — sidecar
                # is shared across many scrapes.
                await browser.close()

    async def aclose(self) -> None:
        # Each scrape opens and closes its own async_playwright + CDP
        # connection, so there is no long-lived resource to release here.
        pass
