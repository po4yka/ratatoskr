"""Direct HTML fetch provider (httpx + trafilatura fallback)."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urljoin

from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.html_utils import html_to_text
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.security.ssrf import is_url_safe, make_safe_async_client

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 30

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru,en-US;q=0.9,en;q=0.8",
}


class DirectHTMLProvider:
    """Tertiary fallback: fetch raw HTML and convert to text."""

    def __init__(
        self,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        *,
        min_text_length: int = 400,
        max_response_mb: int = 10,
    ) -> None:
        self._timeout_sec = timeout_sec
        self._min_text_length = min_text_length
        self._max_response_bytes = max_response_mb * 1024 * 1024

    @property
    def provider_name(self) -> str:
        return "direct_html"

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        started = time.perf_counter()
        try:
            html = await self._fetch_html(url)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.debug(
                "direct_html_fetch_failed",
                extra={
                    "url": redact_url_for_logging(url),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Direct HTML fetch failed: {exc}",
                latency_ms=latency,
                source_url=url,
                endpoint="direct_html",
            )

        latency = int((time.perf_counter() - started) * 1000)

        if not html:
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="Direct HTML: no usable content",
                latency_ms=latency,
                source_url=url,
                endpoint="direct_html",
            )

        content_text = html_to_text(html)
        if len(content_text) < self._min_text_length:
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Direct HTML: content too short ({len(content_text)} chars)",
                content_html=html,
                latency_ms=latency,
                source_url=url,
                endpoint="direct_html",
            )

        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=content_text,
            content_html=html,
            latency_ms=latency,
            source_url=url,
            endpoint="direct_html",
            options_json={"direct_fetch": True},
        )

    async def _fetch_html(self, url: str) -> str | None:
        """Fetch raw HTML with streaming, size limits, and SSRF-safe redirect handling."""
        overall_timeout = self._timeout_sec + 5
        async with asyncio.timeout(overall_timeout):
            async with make_safe_async_client(
                follow_redirects=False, timeout=self._timeout_sec
            ) as client:
                current_url = url
                for _ in range(5):
                    safe, reason = is_url_safe(current_url)
                    if not safe:
                        raise ValueError(f"SSRF blocked redirect target: {reason}")
                    async with client.stream("GET", current_url, headers=_HEADERS) as resp:
                        if resp.status_code in {301, 302, 303, 307, 308}:
                            location = resp.headers.get("location")
                            await resp.aclose()
                            if not location:
                                return None
                            current_url = urljoin(current_url, location)
                            continue

                        ctype = resp.headers.get("content-type", "").lower()
                        if resp.status_code != 200 or "text/html" not in ctype:
                            return None

                        content_length = resp.headers.get("content-length")
                        if content_length:
                            try:
                                if int(content_length) > self._max_response_bytes:
                                    return None
                            except ValueError:
                                logger.debug(
                                    "direct_html_invalid_content_length_header",
                                    extra={"url": current_url, "content_length": content_length},
                                )

                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in resp.aiter_bytes():
                            total += len(chunk)
                            if total > self._max_response_bytes:
                                return None
                            chunks.append(chunk)

                        encoding = resp.encoding or "utf-8"
                        return b"".join(chunks).decode(encoding, errors="replace")
                # Exhausted redirect hops
                raise ValueError("Too many redirects")

    async def aclose(self) -> None:
        pass
