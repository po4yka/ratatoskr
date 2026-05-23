"""CloakBrowser CDP-sidecar content extraction provider.

Connects to a CloakBrowser ``cloakserve`` instance over the Chrome DevTools
Protocol (default ``http://cloakbrowser:9222``). The sidecar runs the upstream
stealth Chromium build with C++ source-level fingerprint patches that apply
automatically across CDP, so we drive it through the standard Playwright API.

Per request we append ``?fingerprint=<seed>&timezone=<tz>&locale=<loc>`` to the
CDP endpoint. ``cloakserve`` spawns one Chrome process per unique seed, so the
seed is derived deterministically from the target's registrable domain — same
domain reuses the same process; different domains get distinct fingerprints
instead of all clustering on cloakserve's default seed.

Humanize is a wrapper-level feature in the upstream package and does NOT apply
over a bare CDP connection. We either (a) call the upstream Python helper if
it is importable, or (b) issue a small bezier mouse-move + wheel sequence on
the page ourselves before reading the HTML.

See ``ops/docker/docker-compose.yml`` for the ``cloakbrowser`` service that
this provider expects under the ``with-scrapers`` profile.
"""

from __future__ import annotations

import hashlib
import random
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from app.adapters.content.scraper.runtime_tuning import tuned_provider_timeout
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.html_utils import html_to_text
from app.core.logging_utils import get_logger
from app.core.url_utils import extract_domain
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

# Picked deterministically per-domain so that a host always sees the same
# (timezone, locale) — same as a real returning user would.
_LOCALE_POOL: tuple[tuple[str, str], ...] = (
    ("UTC", "en-US"),
    ("Europe/Berlin", "de-DE"),
    ("Asia/Tokyo", "ja-JP"),
    ("America/Sao_Paulo", "pt-BR"),
)


def _seed_for_url(url: str) -> str:
    """Deterministic 12-hex-char fingerprint seed keyed on the registrable domain."""
    # extract_domain is the project-wide normalizer; falls back to the raw
    # netloc if parsing fails. The empty-string case is fine — sha1("") is
    # still a valid hex digest.
    domain = (extract_domain(url) or urlsplit(url).netloc or "").lower()
    return hashlib.sha1(domain.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _locale_for_seed(seed: str) -> tuple[str, str]:
    return _LOCALE_POOL[int(seed, 16) % len(_LOCALE_POOL)]


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
        humanize: bool = True,
        proxy: str = "",
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url.rstrip("/")
        self._timeout_sec = timeout_sec
        self._min_text_length = min_text_length
        self._profile = profile
        self._js_heavy_hosts = js_heavy_hosts
        self._humanize = humanize
        self._proxy = proxy
        self._audit = audit

    @property
    def provider_name(self) -> str:
        return "cloakbrowser"

    def _build_cdp_url(self, seed: str, timezone: str, locale: str) -> str:
        params = [
            f"fingerprint={seed}",
            f"timezone={quote(timezone, safe='')}",
            f"locale={quote(locale, safe='')}",
        ]
        if self._proxy:
            params.append(f"proxy={quote(self._proxy, safe='')}")
        return f"{self._endpoint_url}?{'&'.join(params)}"

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

        seed = _seed_for_url(url)
        timezone, locale = _locale_for_seed(seed)
        cdp_url = self._build_cdp_url(seed, timezone, locale)

        effective_timeout = tuned_provider_timeout(
            base_timeout_sec=self._timeout_sec,
            profile=self._profile,
            provider="cloakbrowser",
            url=url,
            js_heavy_hosts=self._js_heavy_hosts,
        )
        timeout_ms = max(1_000, int(effective_timeout * 1000))

        humanize_status = "skipped"
        try:
            html, http_status, humanize_status = await self._render(
                url,
                cdp_url=cdp_url,
                mobile=mobile,
                timeout_ms=timeout_ms,
            )
        except TimeoutError:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "cloakbrowser_timeout",
                extra={
                    "url": url,
                    "timeout_sec": round(effective_timeout, 2),
                    "request_id": request_id,
                    "fingerprint_seed": seed,
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
                        "fingerprint_seed": seed,
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
                    "fingerprint_seed": seed,
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
                        "fingerprint_seed": seed,
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
        stealth_options: dict[str, Any] = {
            "provider": "cloakbrowser",
            "endpoint_url": self._endpoint_url,
            "fingerprint_seed": seed,
            "timezone": timezone,
            "locale": locale,
            "humanize": humanize_status,
            "proxy_configured": bool(self._proxy),
            "mobile": mobile,
        }

        if not html:
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="CloakBrowser: no usable content",
                latency_ms=latency,
                source_url=url,
                endpoint="cloakbrowser",
                options_json=stealth_options,
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
                options_json=stealth_options,
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
                    "fingerprint_seed": seed,
                    "humanize": humanize_status,
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
            options_json=stealth_options,
        )

    async def _render(
        self,
        url: str,
        *,
        cdp_url: str,
        mobile: bool,
        timeout_ms: int,
    ) -> tuple[str | None, int | None, str]:
        """Connect to cloakserve over CDP, render the page, return (html, status, humanize_status)."""
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
            browser = await p.chromium.connect_over_cdp(cdp_url)
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

                    humanize_status = await self._apply_humanize(page) if self._humanize else "skipped"

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
                    return html, http_status, humanize_status
                finally:
                    await context.close()
            finally:
                # Disconnect from cloakserve without shutting it down — sidecar
                # is shared across many scrapes.
                await browser.close()

    async def _apply_humanize(self, page: Any) -> str:
        """Apply post-connect humanize behavior; return the path that ran.

        Upstream's ``humanize=True`` is a Python-wrapper feature and does not
        cross the CDP boundary. We probe for an importable helper first, then
        fall back to an in-house bezier mouse/wheel sequence so behavioral
        signals look non-mechanical to Cloudflare/Turnstile scoring.
        """
        try:
            from cloakbrowser.human import patch_page
        except ImportError:
            patch_page = None

        if patch_page is not None:
            try:
                result = patch_page(page)
                # patch_page may be sync or async depending on upstream version.
                if hasattr(result, "__await__"):
                    await result
                return "patched"
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug(
                    "cloakbrowser_humanize_upstream_helper_failed",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                )

        try:
            await self._humanize_in_house(page)
            return "in_house"
        except Exception as exc:
            logger.debug(
                "cloakbrowser_humanize_in_house_failed",
                extra={"error": str(exc), "error_type": type(exc).__name__},
            )
            return "skipped"

    @staticmethod
    async def _humanize_in_house(page: Any) -> None:
        """Move the mouse along a bezier curve and issue a few wheel deltas.

        Total budget kept under ~200 ms so it does not blow the per-call
        timeout. Deterministic-ish — we use a small random jitter but not the
        per-domain seed, since the goal is "looks human across requests," not
        "looks the same across requests."
        """
        # Bezier mouse path: 4 steps from (x0, y0) → (x3, y3) through (x1,y1),(x2,y2).
        x0, y0 = 50.0, 50.0
        x1, y1 = 200.0 + random.uniform(-30, 30), 150.0 + random.uniform(-30, 30)
        x2, y2 = 450.0 + random.uniform(-40, 40), 320.0 + random.uniform(-40, 40)
        x3, y3 = 600.0 + random.uniform(-40, 40), 500.0 + random.uniform(-40, 40)
        steps = 5
        for i in range(steps + 1):
            t = i / steps
            mt = 1.0 - t
            x = mt**3 * x0 + 3 * mt**2 * t * x1 + 3 * mt * t**2 * x2 + t**3 * x3
            y = mt**3 * y0 + 3 * mt**2 * t * y1 + 3 * mt * t**2 * y2 + t**3 * y3
            await page.mouse.move(x, y, steps=1)

        # 2-4 wheel deltas at varying magnitudes to mimic real scroll bursts.
        for _ in range(random.randint(2, 4)):
            await page.mouse.wheel(0, random.randint(180, 420))

    async def aclose(self) -> None:
        # Each scrape opens and closes its own async_playwright + CDP
        # connection, so there is no long-lived resource to release here.
        pass
