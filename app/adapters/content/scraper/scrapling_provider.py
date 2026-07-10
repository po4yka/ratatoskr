"""Scrapling-based content extraction provider (in-process, zero external deps).

Session lifecycle
-----------------
``ScraplingProvider`` holds a long-lived ``FetcherSession`` for the async basic
(curl_cffi) path.  The session is constructed lazily on the first request and
reused for the lifetime of the provider, giving connection-pool and TLS-handshake
reuse across burst traffic.  ``aclose()`` tears the session down cleanly.

For the stealth (Playwright-backed) path, ``StealthyFetcher`` / ``DynamicFetcher``
are browser-process launchers: each fetch necessarily creates a fresh browser
context.  The fetcher *class* reference is cached on ``self._stealth_fetcher_cls``
so the import only happens once.

Compatibility
-------------
Requires scrapling>=0.4.7.  Older releases lack ``FetcherSession`` and the
``StealthySession``/``AsyncStealthySession`` variants; the provider degrades
gracefully to per-call ``AsyncFetcher`` if the session import fails.
"""

from __future__ import annotations

import asyncio
import os
import time
import weakref
from typing import Any, cast

from app.adapters.content.scraper.runtime_tuning import tuned_provider_timeout
from app.adapters.content.scraper.target_safety import reject_unsafe_target_url
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger
from app.security.ssrf import is_dns_failure_reason, is_url_safe

logger = get_logger(__name__)


def _stealth_max_concurrency() -> int:
    """Max number of concurrent stealth (Playwright/Chromium) browser launches."""
    try:
        return max(1, int(os.getenv("SCRAPLING_STEALTH_MAX_CONCURRENCY", "2")))
    except ValueError:
        return 2


# A stealth fetch launches a full browser process; without a cap, a burst of
# basic-fetch failures could spawn one browser per request and exhaust file
# descriptors / RAM / thread-pool workers. The semaphore is keyed per event loop
# so it binds to the running loop lazily (and stays correct across test loops).
_stealth_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _stealth_launch_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _stealth_semaphores.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_stealth_max_concurrency())
        _stealth_semaphores[loop] = sem
    return sem


class ScraplingProvider:
    """Primary scraper using Scrapling library with TLS impersonation.

    A single ``FetcherSession`` (async curl_cffi session) is constructed lazily
    on the first call to ``_fetch`` and reused for all subsequent requests.
    The stealth Playwright path keeps the fetcher class reference cached but
    always opens a fresh browser context per fetch (required by Playwright).
    Call ``aclose()`` when the provider is no longer needed to release resources.
    """

    def __init__(
        self,
        timeout_sec: int = 30,
        stealth_fallback: bool = True,
        *,
        min_content_length: int = 400,
        profile: str = "balanced",
        js_heavy_hosts: tuple[str, ...] = (),
    ) -> None:
        self._timeout_sec = timeout_sec
        self._stealth_fallback = stealth_fallback
        self._min_content_length = min_content_length
        self._profile = profile
        self._js_heavy_hosts = js_heavy_hosts

        # Lazily initialised async session (FetcherSession context, held open).
        # None = not yet opened; set to the active _ASyncSessionLogic on first use.
        self._async_session: Any = None
        # The FetcherSession context-manager object (kept so __aexit__ can close it).
        self._fetcher_session_ctx: Any = None
        # Guards the lazy-init block in _ensure_async_session against concurrent
        # coroutines racing to open a second session before the first is stored.
        self._session_init_lock: asyncio.Lock = asyncio.Lock()

        # Cached stealth fetcher class reference (import once, reuse across calls).
        self._stealth_fetcher_cls: Any = None

    @property
    def provider_name(self) -> str:
        return "scrapling"

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
            provider="scrapling",
            url=url,
            js_heavy_hosts=self._js_heavy_hosts,
        )
        unsafe_result = await reject_unsafe_target_url(
            provider="scrapling",
            url=url,
            started=started,
            request_id=request_id,
        )
        if unsafe_result is not None:
            return unsafe_result

        try:
            content_html, content_text = await asyncio.wait_for(
                self._fetch(url),
                timeout=timeout_sec,
            )
        except TimeoutError:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "scrapling_timeout",
                extra={"url": url, "timeout_sec": round(timeout_sec, 2)},
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Scrapling timeout after {round(timeout_sec, 2)}s",
                latency_ms=latency,
                source_url=url,
                endpoint="scrapling",
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "scrapling_error",
                extra={"url": url, "error": str(exc), "error_type": type(exc).__name__},
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Scrapling error: {exc}",
                latency_ms=latency,
                source_url=url,
                endpoint="scrapling",
            )

        latency = int((time.perf_counter() - started) * 1000)

        if not content_text or len(content_text) < self._min_content_length:
            logger.info(
                "scrapling_thin_content",
                extra={
                    "url": url,
                    "content_len": len(content_text or ""),
                    "threshold": self._min_content_length,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="Scrapling: insufficient content extracted",
                content_html=content_html,
                latency_ms=latency,
                source_url=url,
                endpoint="scrapling",
            )

        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=content_text,
            content_html=content_html,
            latency_ms=latency,
            source_url=url,
            endpoint="scrapling",
            options_json={"provider": "scrapling"},
        )

    async def _ensure_async_session(self) -> Any:
        """Return the active async session, opening it lazily on first call.

        Tries ``FetcherSession`` (scrapling>=0.4.7) first.  Falls back to the
        module-level ``AsyncFetcher`` singleton (which already pools connections
        internally via curl_cffi) if the import fails.

        Thread-safety: the fast path (session already set) is lock-free.  The
        slow path (first initialisation) is serialised by ``_session_init_lock``
        with a double-checked inner guard so that only one coroutine ever enters
        the ``FetcherSession.__aenter__`` call even under concurrent pressure.
        """
        # Fast path: session already initialised — no lock needed.
        if self._async_session is not None:
            return self._async_session

        # Slow path: serialise concurrent initialisers with the instance lock.
        async with self._session_init_lock:
            # Re-check inside the lock: a concurrent coroutine may have
            # completed initialisation while we were waiting to acquire it.
            if self._async_session is not None:
                return self._async_session

            try:
                import importlib

                mod = importlib.import_module("scrapling.fetchers.requests")
                FetcherSession = getattr(mod, "FetcherSession", None)  # noqa: N806
                if FetcherSession is not None:
                    ctx = FetcherSession()
                    session = await ctx.__aenter__()
                    self._fetcher_session_ctx = ctx
                    self._async_session = session
                    return self._async_session
            except Exception as exc:
                logger.debug(
                    "scrapling_session_init_failed",
                    extra={"error": str(exc), "error_type": type(exc).__name__},
                )

            # Fallback: module-level AsyncFetcher (already a connection-pooling singleton).
            async_fetcher_cls = _lazy_import_async_fetcher()
            self._async_session = async_fetcher_cls  # class itself; callers use .get(url)
            return self._async_session

    async def _fetch(self, url: str) -> tuple[str | None, str | None]:
        """Fetch URL using Scrapling, with optional stealth fallback."""
        loop = asyncio.get_running_loop()

        session = await self._ensure_async_session()
        if session is not None:
            html, text = await _async_fetch_basic(url, session)
        else:
            html, text = await loop.run_in_executor(None, _sync_fetch_basic, url)

        if text and len(text) >= self._min_content_length:
            return html, text

        if self._stealth_fallback:
            logger.debug("scrapling_stealth_fallback", extra={"url": url})
            # Resolve stealth class once; browser context is always per-fetch.
            if self._stealth_fetcher_cls is None:
                self._stealth_fetcher_cls = _lazy_import_stealthy_fetcher()
            stealth_cls = self._stealth_fetcher_cls
            # Cap concurrent browser launches so a burst of fallbacks cannot
            # exhaust file descriptors / RAM / thread-pool workers.
            async with _stealth_launch_semaphore():
                html, text = await loop.run_in_executor(None, _sync_fetch_stealth, url, stealth_cls)

        return html, text

    async def aclose(self) -> None:
        """Release the long-lived async session (if any)."""
        if self._fetcher_session_ctx is not None:
            try:
                await self._fetcher_session_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._fetcher_session_ctx = None
        self._async_session = None


def _lazy_import_fetcher() -> Any:
    """Return a basic Fetcher instance (requires curl_cffi)."""
    import importlib

    mod = importlib.import_module("scrapling")
    return mod.Fetcher()


def _lazy_import_async_fetcher() -> Any:
    """Return the AsyncFetcher class, or None if curl_cffi is unavailable."""
    import importlib

    try:
        mod = importlib.import_module("scrapling.fetchers.requests")
        return getattr(mod, "AsyncFetcher", None)
    except Exception:
        return None


def _lazy_import_stealthy_fetcher() -> Any:
    """Return a callable target whose `.fetch(url)` method works whether the
    target is a class with classmethod `fetch` (DynamicFetcher) or a class with
    instance method `fetch` (StealthyFetcher).

    Always returns the *class* (not an instance) so that callers invoke
    `.fetch(url)` uniformly on either: DynamicFetcher.fetch(url) works because
    fetch is a classmethod; StealthyFetcher.fetch(url) works because fetch is
    also callable on the class (Python binds it to a temporary instance via
    __init_subclass__ protocol in some versions, or it can be called as an
    unbound method).  The caller in _sync_fetch_stealth must NOT call the
    result like a constructor.
    """
    import importlib

    try:
        chrome_mod = importlib.import_module("scrapling.fetchers.chrome")
        cls = getattr(chrome_mod, "DynamicFetcher", None)
        if cls is not None:
            return cls
    except Exception:
        pass

    # Fallback: StealthyFetcher (requires camoufox + curl_cffi).
    # Return the class, not an instance, to keep the .fetch(url) call uniform.
    mod = importlib.import_module("scrapling")
    return mod.StealthyFetcher


# The preflight check in scrape_markdown() only validates the *initial* URL.
# ``follow_redirects="safe"`` is passed explicitly (rather than relying on
# Scrapling's implicit default of the same value) so a future Scrapling/
# curl_cffi upgrade cannot silently widen this to unrestricted redirects.
# curl-impersonate's CURLFOLLOW_SAFE mode rejects any redirect hop whose
# *resolved* connect-time IP falls in a private/internal/link-local range,
# enforced inside libcurl at actual connection time -- this is not a
# preflight-only check and is not vulnerable to DNS-rebinding TOCTOU the way
# a Python-side URL re-validation between hops would be.
_SAFE_REDIRECTS: dict[str, Any] = {"follow_redirects": "safe"}


async def _async_fetch_basic(url: str, session_or_cls: Any) -> tuple[str | None, str | None]:
    """Async basic fetch.

    ``session_or_cls`` is either:
    - an active ``_ASyncSessionLogic`` (from ``FetcherSession.__aenter__``), or
    - the ``AsyncFetcher`` class (module-level singleton fallback).

    Both expose a ``get(url)`` awaitable with the same return shape.
    """
    resp = await session_or_cls.get(url, **_SAFE_REDIRECTS)
    html = resp.text if resp.status == 200 else None
    text = _extract_text(html) if html else None
    return html, text


def _sync_fetch_basic(url: str) -> tuple[str | None, str | None]:
    """Basic fetch via Scrapling Fetcher (TLS impersonation, fastest)."""
    scrapling_fetcher = _lazy_import_fetcher()
    resp = scrapling_fetcher.get(url, **_SAFE_REDIRECTS)
    html = resp.text if resp.status == 200 else None
    text = _extract_text(html) if html else None
    return html, text


def _block_ssrf_route(route: Any) -> None:
    """Abort any stealth-browser request (navigation or subresource) whose
    URL is not SSRF-safe, mirroring the interceptor in playwright_provider.py.

    Installed via Scrapling's ``page_setup`` hook, which -- per Scrapling's
    own docs -- runs on the Playwright ``page`` object *before* navigation
    starts, so it also covers the initial ``goto`` and any redirects it
    triggers, not just post-load subresource requests.

    LIMITATION -- DNS rebinding is not fully mitigated here: ``is_url_safe()``
    resolves the hostname via Python's resolver, but Chromium performs its own
    independent DNS resolution at TCP-connect time. See playwright_provider.py
    for the full residual-risk writeup; the same caveat applies here.
    """
    req_url: str = route.request.url
    safe, reason = is_url_safe(req_url)
    if not safe:
        # A transient resolver hiccup is not a policy block -- label it
        # distinctly (mirrors target_safety.reject_unsafe_target_url) so DNS
        # failures don't masquerade as SSRF rejections in the logs.
        event = (
            "scrapling_stealth_dns_failed"
            if is_dns_failure_reason(reason)
            else "scrapling_stealth_ssrf_blocked"
        )
        logger.warning(event, extra={"url": req_url, "reason": reason})
        route.abort("accessdenied")
        return
    route.continue_()


def _stealth_page_setup(page: Any) -> None:
    """``page_setup`` callback: register the SSRF route guard before navigation."""
    page.route("**/*", _block_ssrf_route)


def _sync_fetch_stealth(url: str, stealth_cls: Any | None = None) -> tuple[str | None, str | None]:
    """Stealth fetch for JS-heavy sites via DynamicFetcher (Playwright-based).

    ``stealth_cls`` is the cached fetcher class (resolved once by the provider).
    Falls back to a fresh import if not supplied.
    """
    if stealth_cls is None:
        stealth_cls = _lazy_import_stealthy_fetcher()
    resp = stealth_cls.fetch(url, solve_cloudflare=True, page_setup=_stealth_page_setup)
    html = resp.text if resp.status == 200 else None
    text = _extract_text(html) if html else None
    return html, text


def _extract_text(html: str) -> str | None:
    """Extract article text from HTML using trafilatura."""
    import importlib

    trafilatura = importlib.import_module("trafilatura")
    return cast(
        "str | None", trafilatura.extract(html, include_comments=False, include_tables=True)
    )
