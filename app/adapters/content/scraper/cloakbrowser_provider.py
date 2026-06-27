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

import asyncio
import hashlib
import random
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlsplit

from app.adapters.content.scraper.runtime_tuning import tuned_provider_timeout
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.html_utils import html_to_text
from app.core.logging_utils import get_logger
from app.core.url_utils import extract_domain
from app.security.ssrf import is_url_safe_async

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

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


# In-page scan for download controls (tier-2 agentic recovery). Returns anchors
# and buttons with their visible text + resolved href, capped so a hostile page
# can't flood the picker. Kept as a string literal so it can ship without a JS
# build step.
_CONTROL_SCAN_JS = """
() => {
  const out = [];
  const pick = (el) => (el.innerText || el.getAttribute('aria-label') || el.title || '').trim().slice(0, 160);
  for (const a of document.querySelectorAll('a[href]')) {
    out.push({tag: 'a', text: pick(a), href: a.href});
    if (out.length >= 200) return out;
  }
  for (const b of document.querySelectorAll('button, [role="button"], input[type="submit"]')) {
    out.push({tag: 'button', text: pick(b), href: null});
    if (out.length >= 200) return out;
  }
  return out;
}
"""


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

        safe, reason = await is_url_safe_async(url)
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
                        {"width": 390, "height": 844} if mobile else {"width": 1366, "height": 768}
                    ),
                    is_mobile=mobile,
                    has_touch=mobile,
                )
                try:
                    page = await context.new_page()

                    async def _block_ssrf_route(route: Any) -> None:
                        req_url: str = route.request.url
                        # Playwright route callbacks are async, so we can await
                        # is_url_safe_async to keep DNS off the event loop.
                        ok, why = await is_url_safe_async(req_url)
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

                    humanize_status = (
                        await self._apply_humanize(page) if self._humanize else "skipped"
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

    async def fetch_pdf(
        self,
        landing_url: str,
        pdf_url: str,
        *,
        max_bytes: int,
        mobile: bool = False,
    ) -> bytes | None:
        """Fetch ``pdf_url`` through a stealth session that first clears Cloudflare.

        The cookie-less httpx download in the academic extractor cannot carry the
        ``cf_clearance`` cookie minted when the landing page passes the challenge, so
        a gated PDF (SSRN ``Delivery.cfm``) 403s even though it is public. Here we
        render the landing page in a fresh CloakBrowser context — minting the
        clearance cookie into that context's jar — then navigate to the PDF in the
        SAME context so the cookie travels with the request.

        Returns the raw PDF bytes, or ``None`` on any miss (SSRF block, non-PDF
        body — e.g. Cloudflare served a second challenge — oversize, or error).
        Never raises: the caller degrades to the next recovery tier.
        """
        safe, reason = await is_url_safe_async(pdf_url)
        if not safe:
            logger.warning(
                "cloakbrowser_pdf_ssrf_blocked", extra={"url": pdf_url, "reason": reason}
            )
            return None

        seed = _seed_for_url(landing_url)
        timeout_ms = max(1_000, int(self._timeout_sec * 1000))
        try:
            async with self._stealth_page(landing_url, seed=seed, mobile=mobile) as (
                page,
                downloads,
            ):
                body = await self._goto_capture(
                    page, pdf_url, downloads, timeout_ms=timeout_ms, max_bytes=max_bytes
                )
            return self._validate_pdf(body, pdf_url)
        except Exception as exc:
            logger.warning(
                "cloakbrowser_pdf_fetch_failed",
                extra={
                    "landing_url": landing_url,
                    "pdf_url": pdf_url,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "fingerprint_seed": seed,
                },
            )
            return None

    async def download_pdf_via_controls(
        self,
        landing_url: str,
        *,
        picker: Callable[[list[dict[str, Any]]], Awaitable[dict[str, Any] | None]],
        max_bytes: int,
        mobile: bool = False,
    ) -> bytes | None:
        """Tier-2 agentic download: render the page, let ``picker`` choose a control.

        Used for hosts with no deterministic PDF URL. The page is rendered in a
        stealth context; ``picker`` receives the live anchor/button candidates and
        returns the one that downloads the paper (by ``href`` or visible text). An
        ``href`` candidate is fetched through the session (reusing the cleared
        Cloudflare cookie); a button is clicked inside ``expect_download``. Never
        raises.
        """
        safe, reason = await is_url_safe_async(landing_url)
        if not safe:
            logger.warning(
                "cloakbrowser_agentic_ssrf_blocked",
                extra={"url": landing_url, "reason": reason},
            )
            return None

        seed = _seed_for_url(landing_url)
        timeout_ms = max(1_000, int(self._timeout_sec * 1000))
        try:
            async with self._stealth_page(landing_url, seed=seed, mobile=mobile) as (
                page,
                downloads,
            ):
                candidates = await page.evaluate(_CONTROL_SCAN_JS)
                choice = await picker(list(candidates or []))
                if not choice:
                    return None
                href = choice.get("href")
                if href:
                    ok, _why = await is_url_safe_async(href)
                    if not ok:
                        return None
                    body = await self._goto_capture(
                        page, href, downloads, timeout_ms=timeout_ms, max_bytes=max_bytes
                    )
                else:
                    body = await self._click_capture(
                        page, choice, downloads, timeout_ms=timeout_ms, max_bytes=max_bytes
                    )
            return self._validate_pdf(body, href or landing_url)
        except Exception as exc:
            logger.warning(
                "cloakbrowser_agentic_failed",
                extra={
                    "landing_url": landing_url,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "fingerprint_seed": seed,
                },
            )
            return None

    @asynccontextmanager
    async def _stealth_page(
        self, landing_url: str, *, seed: str, mobile: bool
    ) -> AsyncIterator[tuple[Any, list[Any]]]:
        """Connect over CDP, open a downloads-enabled page, clear CF on the landing.

        Yields ``(page, downloads)`` where ``downloads`` accumulates any download
        events. Tears down the context + CDP connection (without stopping the
        shared sidecar) on exit.
        """
        from playwright.async_api import (
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
            async_playwright,
        )

        timezone, locale = _locale_for_seed(seed)
        cdp_url = self._build_cdp_url(seed, timezone, locale)
        timeout_ms = max(1_000, int(self._timeout_sec * 1000))

        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(cdp_url)
            try:
                context = await browser.new_context(
                    user_agent=_MOBILE_UA if mobile else _DESKTOP_UA,
                    viewport=(
                        {"width": 390, "height": 844} if mobile else {"width": 1366, "height": 768}
                    ),
                    is_mobile=mobile,
                    has_touch=mobile,
                    accept_downloads=True,
                )
                try:
                    page = await context.new_page()
                    downloads: list[Any] = []
                    # NB: a bound builtin method (downloads.append) cannot be used
                    # directly — Playwright setattr()s the handler, which fails on
                    # builtins. Wrap in a lambda.
                    page.on("download", lambda d: downloads.append(d))

                    async def _block_ssrf_route(route: Any) -> None:
                        ok, _why = await is_url_safe_async(route.request.url)
                        if not ok:
                            await route.abort("accessdenied")
                            return
                        await route.continue_()

                    await page.route("**/*", _block_ssrf_route)

                    # Clear Cloudflare on the landing page → mints cf_clearance
                    # into the context jar for the subsequent PDF request. The
                    # networkidle settle gives a managed challenge a chance to
                    # resolve + redirect before we navigate to the PDF (mirrors
                    # scrape_markdown). A strict managed challenge that never
                    # clears just leaves the PDF gated → graceful None.
                    try:
                        await page.goto(
                            landing_url, wait_until="domcontentloaded", timeout=timeout_ms
                        )
                        if self._humanize:
                            await self._apply_humanize(page)
                        await page.wait_for_load_state(
                            "networkidle", timeout=min(8_000, timeout_ms)
                        )
                    except (PlaywrightTimeoutError, PlaywrightError):
                        logger.debug(
                            "cloakbrowser_pdf_landing_partial",
                            extra={"landing_url": landing_url},
                            exc_info=True,
                        )

                    yield page, downloads
                finally:
                    await context.close()
            finally:
                await browser.close()

    async def _goto_capture(
        self, page: Any, url: str, downloads: list[Any], *, timeout_ms: int, max_bytes: int
    ) -> bytes | None:
        """Fetch PDF bytes through the stealth context.

        Primary path: the context's APIRequestContext (``context.request``) —
        reuses the context cookies (incl. ``cf_clearance``) and returns the RAW
        response body. A plain ``page.goto`` to an inline ``application/pdf`` is
        wrong: Chrome hands it to its built-in PDF viewer and ``response.body()``
        yields the viewer's HTML shell, not the file. Fallback: a navigation that
        triggers a forced download (hosts that send ``Content-Disposition:
        attachment``), captured via the download event.
        """
        from playwright.async_api import Error as PlaywrightError, TimeoutError as PWTimeout

        try:
            resp = await page.context.request.get(url, timeout=timeout_ms)
            body = await resp.body()
            if body and len(body) <= max_bytes and body.lstrip().startswith(b"%PDF"):
                return body
        except (PWTimeout, PlaywrightError):
            logger.debug("cloakbrowser_pdf_request_fetch_failed", extra={"url": url}, exc_info=True)

        # Fallback: forced-download navigation (Content-Disposition: attachment).
        downloads.clear()
        try:
            await page.goto(url, wait_until="commit", timeout=timeout_ms)
        except (PWTimeout, PlaywrightError):
            logger.debug("cloakbrowser_pdf_goto_aborted", extra={"url": url}, exc_info=True)
        return await self._read_pdf_body(downloads, None, max_bytes=max_bytes)

    async def _click_capture(
        self,
        page: Any,
        choice: dict[str, Any],
        downloads: list[Any],
        *,
        timeout_ms: int,
        max_bytes: int,
    ) -> bytes | None:
        """Click a text-identified control and capture the resulting download."""
        from playwright.async_api import Error as PlaywrightError, TimeoutError as PWTimeout

        text = (choice.get("text") or "").strip()
        if not text:
            return None
        downloads.clear()
        try:
            async with page.expect_download(timeout=timeout_ms):
                await page.get_by_text(text, exact=False).first.click(timeout=timeout_ms)
        except (PWTimeout, PlaywrightError):
            return None
        return await self._read_pdf_body(downloads, None, max_bytes=max_bytes)

    @staticmethod
    async def _read_pdf_body(
        downloads: list[Any], response: Any, *, max_bytes: int
    ) -> bytes | None:
        """Read the PDF bytes from either a captured download or the goto response."""
        if downloads:
            path = await downloads[0].path()
            if path is None:
                return None
            data = await asyncio.to_thread(path.read_bytes)
            return data if 0 < len(data) <= max_bytes else None
        if response is None:
            return None
        body = await response.body()
        if not body or len(body) > max_bytes:
            return None
        return body

    @staticmethod
    def _validate_pdf(body: bytes | None, source: str) -> bytes | None:
        """Return ``body`` only if it is non-empty PDF bytes, else ``None``."""
        if body is None:
            return None
        if not body.lstrip().startswith(b"%PDF"):
            logger.info("cloakbrowser_pdf_not_a_pdf", extra={"source": source, "bytes": len(body)})
            return None
        return body

    async def aclose(self) -> None:
        # Each scrape opens and closes its own async_playwright + CDP
        # connection, so there is no long-lived resource to release here.
        pass
