"""Crawl4AI REST API content extraction provider.

Upstream wire contract (Crawl4AI Docker server v0.8.x):
  POST /crawl  ->  {"success": bool, "results": [{"success": bool, "markdown": ..., ...}]}

Reference: https://github.com/unclecode/crawl4ai/blob/main/deploy/docker/README.md

We use POST /crawl with stream=False so results are available in a single round-trip.
The body envelopes nested configs in {type, params} wrappers as required by the v0.8.x API.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

from app.adapters.content.scraper.runtime_tuning import tuned_provider_timeout
from app.adapters.content.scraper.target_safety import reject_unsafe_target_url
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger
from app.security.ssrf import is_url_safe

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 60
_DEFAULT_API_BASE_URL = "http://crawl4ai:11235"


class Crawl4AIProvider:
    """Content extraction via the Crawl4AI REST API (self-hosted Docker sidecar).

    Sends a crawl request to POST /crawl/sync and extracts markdown content.
    Returns a FirecrawlResult with the extracted content or an error description.
    """

    def __init__(
        self,
        url: str,
        token: str = "",
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        *,
        min_content_length: int = 400,
        profile: str = "balanced",
        js_heavy_hosts: tuple[str, ...] = (),
        cache_mode: str = "BYPASS",
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._timeout_sec = timeout_sec
        self._min_content_length = min_content_length
        self._profile = profile
        self._js_heavy_hosts = js_heavy_hosts
        self._cache_mode = cache_mode
        self._audit = audit
        self._client: httpx.AsyncClient | None = None

    @property
    def provider_name(self) -> str:
        return "crawl4ai"

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                follow_redirects=False,
                timeout=self._timeout_sec,
            )
        return self._client

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        del mobile  # Crawl4AI browser config does not expose mobile/desktop distinction

        started = time.perf_counter()
        effective_timeout = tuned_provider_timeout(
            base_timeout_sec=self._timeout_sec,
            profile=self._profile,
            provider="crawl4ai",
            url=url,
            js_heavy_hosts=self._js_heavy_hosts,
        )
        unsafe_result = await reject_unsafe_target_url(
            provider="crawl4ai",
            url=url,
            started=started,
            request_id=request_id,
        )
        if unsafe_result is not None:
            return unsafe_result

        payload: dict[str, Any] = {
            "urls": [url],
            "browser_config": {
                "type": "BrowserConfig",
                "params": {"headless": True, "user_agent_mode": "random"},
            },
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {
                    "cache_mode": self._cache_mode,
                    "stream": False,
                },
            },
        }

        # POST /crawl with stream=False returns results immediately (v0.8.x API).
        crawl_endpoint = f"{self._url}/crawl"

        try:
            client = self._get_client()
            current_endpoint = crawl_endpoint
            data: dict[str, Any] | None = None
            for _ in range(5):
                if current_endpoint != crawl_endpoint:
                    safe, reason = is_url_safe(current_endpoint)
                    if not safe:
                        raise ValueError(f"SSRF blocked redirect target: {reason}")
                resp = await client.post(
                    current_endpoint,
                    json=payload,
                    headers=self._build_headers(),
                    timeout=effective_timeout,
                )
                if resp.status_code in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location")
                    if not location:
                        resp.raise_for_status()
                    current_endpoint = urljoin(current_endpoint, location)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            else:
                raise ValueError("Too many redirects")
        except (TimeoutError, httpx.TimeoutException):
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "crawl4ai_timeout",
                extra={"url": url, "timeout_sec": effective_timeout, "request_id": request_id},
            )
            if self._audit:
                self._audit(
                    "ERROR",
                    "crawl4ai_failure",
                    {
                        "url": url,
                        "error": "timeout",
                        "timeout_sec": effective_timeout,
                        "request_id": request_id,
                    },
                )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Crawl4AI timeout after {effective_timeout:.0f}s",
                latency_ms=latency,
                source_url=url,
                endpoint="crawl4ai",
            )
        except httpx.HTTPStatusError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "crawl4ai_http_error",
                extra={
                    "url": url,
                    "status_code": exc.response.status_code,
                    "request_id": request_id,
                },
            )
            if self._audit:
                self._audit(
                    "ERROR",
                    "crawl4ai_failure",
                    {
                        "url": url,
                        "error": f"HTTP {exc.response.status_code}",
                        "request_id": request_id,
                    },
                )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Crawl4AI HTTP {exc.response.status_code}",
                http_status=exc.response.status_code,
                latency_ms=latency,
                source_url=url,
                endpoint="crawl4ai",
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.debug(
                "crawl4ai_fetch_failed",
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
                    "crawl4ai_failure",
                    {
                        "url": url,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "request_id": request_id,
                    },
                )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Crawl4AI fetch failed: {exc}",
                latency_ms=latency,
                source_url=url,
                endpoint="crawl4ai",
            )

        latency = int((time.perf_counter() - started) * 1000)

        results = data.get("results") if isinstance(data, dict) else None
        if not results or not isinstance(results, list):
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="Crawl4AI: empty or missing results array",
                latency_ms=latency,
                source_url=url,
                endpoint="crawl4ai",
            )

        first = results[0]
        if not first.get("success", False):
            error_detail = first.get("error", "unknown error")
            logger.info(
                "crawl4ai_result_failed",
                extra={"url": url, "error": error_detail, "request_id": request_id},
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Crawl4AI reported failure: {error_detail}",
                latency_ms=latency,
                source_url=url,
                endpoint="crawl4ai",
            )

        raw_markdown = first.get("markdown")
        if isinstance(raw_markdown, dict):
            # Some Crawl4AI versions return {"fit_markdown": "...", "raw_markdown": "..."}
            content_markdown = (
                raw_markdown.get("fit_markdown") or raw_markdown.get("raw_markdown") or ""
            )
        elif isinstance(raw_markdown, str):
            content_markdown = raw_markdown
        else:
            content_markdown = ""

        if not content_markdown or len(content_markdown.strip()) < self._min_content_length:
            logger.info(
                "crawl4ai_thin_content",
                extra={
                    "url": url,
                    "content_len": len(content_markdown.strip()) if content_markdown else 0,
                    "threshold": self._min_content_length,
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=(
                    f"Crawl4AI: content too short "
                    f"({len(content_markdown.strip()) if content_markdown else 0} chars)"
                ),
                latency_ms=latency,
                source_url=url,
                endpoint="crawl4ai",
            )

        metadata = first.get("metadata") or {}

        if self._audit:
            self._audit(
                "INFO",
                "crawl4ai_request",
                {
                    "url": url,
                    "latency_ms": latency,
                    "content_len": len(content_markdown.strip()),
                    "request_id": request_id,
                },
            )

        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=content_markdown.strip(),
            metadata_json=metadata if isinstance(metadata, dict) else None,
            latency_ms=latency,
            source_url=url,
            endpoint="crawl4ai",
            options_json={"provider": "crawl4ai", "url_endpoint": self._url},
        )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
