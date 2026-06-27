"""Authenticated, persistent CloakBrowser context + in-context HTTP fetcher.

The scraper ``CloakBrowserProvider`` is stateless (a fresh anonymous context per
scrape). This module adds the missing piece: a context that pre-loads a saved
``storage_state`` (cookies + localStorage), so a logged-in ChatGPT/Claude session
survives across runs, plus an ``AuthedFetcher`` that issues internal-API GETs
through the browser's own ``APIRequestContext``.

Why fetch through the browser context rather than httpx: Cloudflare binds the
``cf_clearance`` cookie to the TLS/JA3 fingerprint and source IP of the session
that solved the challenge. ``page.context.request`` reuses the stealth Chromium's
cookie jar *and* TLS fingerprint, so the clearance cookie stays valid; a separate
Python TLS stack would present a different JA3 and get re-challenged.

This module is intentionally decoupled from the ``ai_backup`` feature: it raises
its own exceptions and does its own host matching so it can be reused.
"""

from __future__ import annotations

import asyncio
import json
import random
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from app.adapters.content.scraper.fingerprint import (
    DESKTOP_UA,
    MOBILE_UA,
    build_cdp_url,
    locale_for_seed,
    seed_for_url,
)
from app.core.logging_utils import get_logger
from app.security.ssrf import is_url_safe_async

logger = get_logger(__name__)

# Redirects are followed manually so the host allowlist + SSRF guard re-run on
# every hop (auto-following would let a 3xx escape the allowlist -> SSRF).
_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


# ── Fetcher-layer exceptions ─────────────────────────────────────────────────


class HostNotAllowedError(ValueError):
    """The URL's host is not in the fetcher's host allowlist."""


class SSRFBlockedError(ValueError):
    """``is_url_safe_async`` rejected this URL."""


class RequestCapExceededError(RuntimeError):
    """The per-run request cap was reached."""


# ── Response wrapper ─────────────────────────────────────────────────────────


@dataclass
class FetchResponse:
    """An HTTP response whose body bytes were already awaited at construction."""

    status: int
    body_bytes: bytes

    def json(self) -> Any:
        return json.loads(self.body_bytes)

    def bytes(self) -> bytes:
        return self.body_bytes


class AuthedFetcher(Protocol):
    """Structural protocol every client depends on (and every test fakes)."""

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse: ...


# ── Host matching (wildcard-aware, self-contained) ───────────────────────────


def _host_in_allowlist(host: str, patterns: list[str]) -> bool:
    host = host.lower()
    for pattern in patterns:
        p = pattern.lower()
        if p.startswith("*."):
            suffix = p[2:]
            if host == suffix or host.endswith("." + suffix):
                return True
        elif host == p:
            return True
    return False


@asynccontextmanager
async def authenticated_context(
    domain: str,
    storage_state: dict | None,
    *,
    endpoint_url: str,
    mobile: bool = False,
    proxy: str = "",
    refreshed_out: list[dict] | None = None,
) -> AsyncIterator[tuple[Any, Any]]:
    """Connect to cloakserve for ``domain`` and yield ``(page, context)``.

    - Pins a deterministic fingerprint seed derived from ``domain`` so the site
      always reuses the same cloakserve Chrome process / fingerprint.
    - Pre-loads ``storage_state`` (pass ``None`` for a fresh anonymous session).
    - Keeps the SSRF route guard on page-driven sub-requests (this does NOT cover
      ``context.request`` calls — the fetcher guards those independently).
    - On exit, exports the refreshed storage_state into ``refreshed_out[0]``
      BEFORE closing the context (the jar is gone after ``context.close()``), then
      disconnects from the shared sidecar without stopping it.
    """
    seed = seed_for_url(f"https://{domain}")
    timezone, locale = locale_for_seed(seed)
    cdp_url = build_cdp_url(endpoint_url, seed, timezone, locale, proxy=proxy)

    from playwright.async_api import Error as PWError, async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        try:
            ctx_kwargs: dict[str, Any] = {
                "user_agent": MOBILE_UA if mobile else DESKTOP_UA,
                "viewport": {"width": 390, "height": 844}
                if mobile
                else {"width": 1366, "height": 768},
                "is_mobile": mobile,
                "has_touch": mobile,
                "accept_downloads": True,
            }
            if storage_state is not None:
                ctx_kwargs["storage_state"] = storage_state
            context = await browser.new_context(**ctx_kwargs)
            try:
                page = await context.new_page()

                async def _ssrf_guard(route: Any) -> None:
                    ok, why = await is_url_safe_async(route.request.url)
                    if not ok:
                        logger.warning(
                            "auth_ctx_ssrf_blocked",
                            extra={"url": route.request.url, "reason": why},
                        )
                        await route.abort("accessdenied")
                        return
                    await route.continue_()

                await page.route("**/*", _ssrf_guard)
                yield page, context
            finally:
                # Export BEFORE close — the cookie jar is destroyed by close().
                try:
                    refreshed = await context.storage_state()
                    if refreshed_out is not None:
                        refreshed_out.append(dict(refreshed))
                except PWError:
                    logger.warning("auth_ctx_storage_state_export_failed")
                finally:
                    # Always close, even if storage_state() raised a non-PWError.
                    await context.close()
        finally:
            # CDP disconnect only — never stop the shared cloakserve sidecar.
            await browser.close()


class PlaywrightAuthedFetcher:
    """``AuthedFetcher`` backed by a Playwright ``APIRequestContext``.

    Enforcement order (cheapest first): host allowlist (sync) -> SSRF guard
    (async DNS) -> per-run request cap -> inter-request delay + jitter (skipped on
    the first request).
    """

    def __init__(
        self,
        context: Any,
        *,
        host_allowlist: list[str],
        inter_request_delay_sec: float = 1.5,
        jitter_sec: float = 0.5,
        max_requests: int = 5000,
        timeout_ms: int = 30_000,
    ) -> None:
        self._req = context.request
        self._allowlist = list(host_allowlist)
        self._delay = inter_request_delay_sec
        self._jitter = jitter_sec
        self._max = max_requests
        self._timeout_ms = timeout_ms
        self._count = 0

    @property
    def requests_made(self) -> int:
        return self._count

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse:
        """GET ``url``, re-validating the allowlist + SSRF guard on every redirect hop.

        Redirects are NOT followed automatically (``max_redirects=0``); each 3xx
        ``Location`` is resolved and re-checked before the next request, so a
        302 to an internal/disallowed host is refused rather than followed.
        """
        target = url
        for _ in range(_MAX_REDIRECTS + 1):
            host = (urlparse(target).hostname or "").lower()
            if not _host_in_allowlist(host, self._allowlist):
                raise HostNotAllowedError(f"{host!r} not in host allowlist")

            ok, why = await is_url_safe_async(target)
            if not ok:
                raise SSRFBlockedError(why or "SSRF blocked")

            if self._count >= self._max:
                raise RequestCapExceededError(f"request cap {self._max} reached")

            if self._count > 0:
                await asyncio.sleep(self._delay + random.uniform(0.0, self._jitter))

            self._count += 1
            resp = await self._req.get(
                target, headers=headers or {}, timeout=self._timeout_ms, max_redirects=0
            )
            if resp.status in _REDIRECT_STATUSES:
                location = (resp.headers or {}).get("location")
                if location:
                    target = urljoin(target, location)
                    continue
                # 3xx without a Location: surface it to the caller as-is.
            return FetchResponse(status=resp.status, body_bytes=await resp.body())

        raise SSRFBlockedError(f"too many redirects (> {_MAX_REDIRECTS})")


__all__ = [
    "AuthedFetcher",
    "FetchResponse",
    "HostNotAllowedError",
    "PlaywrightAuthedFetcher",
    "RequestCapExceededError",
    "SSRFBlockedError",
    "authenticated_context",
]
