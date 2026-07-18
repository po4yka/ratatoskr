"""Authenticated, persistent CloakBrowser context + in-context HTTP fetcher.

The scraper ``CloakBrowserProvider`` is stateless (a fresh anonymous context per
scrape). This module adds the missing piece: a context that pre-loads a saved
``storage_state`` (cookies + localStorage), so a logged-in ChatGPT/Claude session
survives across runs, plus an ``AuthedFetcher`` that issues internal-API GETs
through the browser's own network stack and reads response bodies as bounded
Chrome DevTools Protocol streams.

Why fetch through the browser context rather than httpx: Cloudflare binds the
``cf_clearance`` cookie to the TLS/JA3 fingerprint and source IP of the session
that solved the challenge. Page navigation reuses the stealth Chromium's cookie
jar *and* TLS fingerprint, so the clearance cookie stays valid; a separate Python
TLS stack would present a different JA3 and get re-challenged.

This module is intentionally decoupled from the ``ai_backup`` feature: it raises
its own exceptions and does its own host matching so it can be reused.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import random
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

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
_STREAM_CHUNK_BYTES = 64 * 1024


# ── Fetcher-layer exceptions ─────────────────────────────────────────────────


class HostNotAllowedError(ValueError):
    """The URL's host is not in the fetcher's host allowlist."""


class SSRFBlockedError(ValueError):
    """``is_url_safe_async`` rejected this URL."""


class RequestCapExceededError(RuntimeError):
    """The per-run request cap was reached."""


class ResponseCapExceededError(RequestCapExceededError):
    """A response or the aggregate run exceeded its configured byte cap."""


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
    - Keeps the SSRF route guard on page-driven sub-requests; the fetcher also
      guards each requested URL and redirect before Chromium transports it.
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
    """``AuthedFetcher`` backed by a bounded Chromium response stream.

    Enforcement order (cheapest first): host allowlist (sync) -> SSRF guard
    (async DNS) -> per-run request cap -> inter-request delay + jitter (skipped on
    the first request). The body is read via CDP ``IO.read`` because Playwright's
    public ``APIResponse.body()`` API materializes the entire response before a
    caller can inspect its size.
    """

    def __init__(
        self,
        page: Any,
        *,
        host_allowlist: list[str],
        inter_request_delay_sec: float = 1.5,
        jitter_sec: float = 0.5,
        max_requests: int = 5000,
        max_response_bytes: int = 64 * 1024 * 1024,
        max_run_bytes: int = 2 * 1024 * 1024 * 1024,
        timeout_ms: int = 30_000,
    ) -> None:
        self._page = page
        self._context = page.context
        self._allowlist = list(host_allowlist)
        self._delay = inter_request_delay_sec
        self._jitter = jitter_sec
        self._max = max_requests
        self._max_response_bytes = max_response_bytes
        self._max_run_bytes = max_run_bytes
        self._timeout_ms = timeout_ms
        self._count = 0
        self._bytes_received = 0
        self._session: Any | None = None
        self._lock = asyncio.Lock()

    @property
    def requests_made(self) -> int:
        return self._count

    @property
    def bytes_received(self) -> int:
        return self._bytes_received

    def _check_declared_size(self, headers: dict[str, str]) -> None:
        raw = headers.get("content-length")
        if raw is None:
            return
        try:
            declared = int(raw)
        except (TypeError, ValueError):
            return
        if declared > self._max_response_bytes:
            raise ResponseCapExceededError(
                f"response Content-Length {declared} exceeds cap {self._max_response_bytes}"
            )
        if self._bytes_received + declared > self._max_run_bytes:
            raise ResponseCapExceededError(
                f"run response bytes would exceed cap {self._max_run_bytes}"
            )

    async def _validate_target(self, target: str) -> None:
        host = (urlparse(target).hostname or "").lower()
        if not _host_in_allowlist(host, self._allowlist):
            raise HostNotAllowedError(f"{host!r} not in host allowlist")

        ok, why = await is_url_safe_async(target)
        if not ok:
            raise SSRFBlockedError(why or "SSRF blocked")

    async def _begin_request(self, target: str) -> None:
        await self._validate_target(target)

        if self._count >= self._max:
            raise RequestCapExceededError(f"request cap {self._max} reached")

        if self._count > 0:
            await asyncio.sleep(self._delay + random.uniform(0.0, self._jitter))
        self._count += 1

    @staticmethod
    def _response_headers(event: dict[str, Any]) -> dict[str, str]:
        return {
            str(header.get("name", "")).lower(): str(header.get("value", ""))
            for header in event.get("responseHeaders") or []
            if isinstance(header, dict)
        }

    async def _read_stream(self, session: Any, request_id: str) -> bytes:
        result = await session.send("Fetch.takeResponseBodyAsStream", {"requestId": request_id})
        handle = result["stream"]
        body = bytearray()
        try:
            while True:
                response_remaining = self._max_response_bytes - len(body)
                run_remaining = self._max_run_bytes - self._bytes_received - len(body)
                remaining = min(response_remaining, run_remaining)
                # One byte past the remaining budget is enough to prove overflow;
                # never ask Chromium to hand Python an unbounded chunk.
                read_size = min(_STREAM_CHUNK_BYTES, max(1, remaining + 1))
                part = await session.send("IO.read", {"handle": handle, "size": read_size})
                raw = part.get("data", "")
                try:
                    chunk = (
                        base64.b64decode(raw, validate=True)
                        if part.get("base64Encoded")
                        else str(raw).encode("utf-8")
                    )
                except (binascii.Error, ValueError) as exc:
                    raise RuntimeError("browser returned an invalid response stream chunk") from exc

                if len(chunk) > response_remaining:
                    raise ResponseCapExceededError(
                        f"response body exceeds cap {self._max_response_bytes}"
                    )
                if len(chunk) > run_remaining:
                    raise ResponseCapExceededError(
                        f"run response bytes exceed cap {self._max_run_bytes}"
                    )
                body.extend(chunk)
                if part.get("eof"):
                    break
        finally:
            with suppress(Exception):
                await session.send("IO.close", {"handle": handle})

        self._bytes_received += len(body)
        return bytes(body)

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse:
        """GET ``url`` through Chromium, streaming and bounding the body in Python.

        CDP pauses the initial request and every redirect before transport, so
        each hop is re-checked against the host allowlist and SSRF guard. The
        terminal response is paused before its body is received and exposed as a
        sequential stream; it is never materialized by ``APIResponse.body()``.
        """
        async with self._lock:
            return await self._get_locked(url, headers=headers)

    async def _get_locked(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> FetchResponse:
        await self._page.set_extra_http_headers(headers or {})
        await self._begin_request(url)

        if self._session is None:
            self._session = await self._context.new_cdp_session(self._page)
        session = self._session
        paused: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        listener = paused.put_nowait
        session.on("Fetch.requestPaused", listener)
        paused_request_id: str | None = None
        stream_taken = False
        redirects = 0
        first_request = True
        navigate_task: asyncio.Task[dict[str, Any]] | None = None

        try:
            await session.send(
                "Fetch.enable",
                {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
            )
            # Page.navigate does not resolve while Fetch has its request paused;
            # drive it concurrently with the pause/continue event loop below.
            navigate_task = asyncio.create_task(session.send("Page.navigate", {"url": url}))

            async with asyncio.timeout(self._timeout_ms / 1000.0):
                while True:
                    event = await paused.get()
                    paused_request_id = str(event["requestId"])
                    is_response = "responseStatusCode" in event or "responseErrorReason" in event
                    if not is_response:
                        target = str((event.get("request") or {}).get("url", ""))
                        if first_request:
                            # The exact requested URL was checked before navigation;
                            # still fail closed if Chromium rewrites it unexpectedly.
                            first_request = False
                            if target != url:
                                await self._validate_target(target)
                        else:
                            redirects += 1
                            if redirects > _MAX_REDIRECTS:
                                raise SSRFBlockedError(f"too many redirects (> {_MAX_REDIRECTS})")
                            await self._begin_request(target)
                        await session.send(
                            "Fetch.continueRequest",
                            {"requestId": paused_request_id, "interceptResponse": True},
                        )
                        paused_request_id = None
                        continue

                    if event.get("responseErrorReason"):
                        raise OSError("authenticated browser transport failed")

                    status = int(event.get("responseStatusCode", 0))
                    response_headers = self._response_headers(event)
                    if status in _REDIRECT_STATUSES and "location" in response_headers:
                        await session.send(
                            "Fetch.continueRequest", {"requestId": paused_request_id}
                        )
                        paused_request_id = None
                        continue

                    self._check_declared_size(response_headers)
                    stream_taken = True
                    body = await self._read_stream(session, paused_request_id)
                    return FetchResponse(status=status, body_bytes=body)
        finally:
            if paused_request_id is not None:
                # Once a response stream is taken it cannot be continued. We do
                # not need the navigation body in the page, so cancellation is
                # the bounded completion path after copying the accepted bytes.
                with suppress(Exception):
                    await session.send(
                        "Fetch.failRequest",
                        {"requestId": paused_request_id, "errorReason": "Aborted"},
                    )
            session.remove_listener("Fetch.requestPaused", listener)
            with suppress(Exception):
                await session.send("Fetch.disable")
            if stream_taken:
                with suppress(Exception):
                    await session.send("Page.stopLoading")
            if navigate_task is not None:
                navigate_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await navigate_task


__all__ = [
    "AuthedFetcher",
    "FetchResponse",
    "HostNotAllowedError",
    "PlaywrightAuthedFetcher",
    "RequestCapExceededError",
    "ResponseCapExceededError",
    "SSRFBlockedError",
    "authenticated_context",
]
