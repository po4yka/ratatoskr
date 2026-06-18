"""Microsoft Webwright last-resort browser-agent provider.

Calls the Webwright sidecar (ops/docker/webwright/) which runs an LLM-driven
Playwright agent loop against the URL. This is an order-of-magnitude more
expensive than every other rung in the chain, so the provider self-gates on a
host allowlist: if the URL's host is not in WEBWRIGHT_HOST_ALLOWLIST, it
returns ERROR immediately so the chain attempt log shows the skip without
incurring any sidecar work.

The provider preserves the request's correlation_id via the X-Correlation-Id
header so the sidecar's trajectories/logs can be joined back to the originating
Ratatoskr request (Operating Rule 1). The sidecar's trajectory directory path
is attached to ``FirecrawlResult.options_json`` under
``_webwright_trajectory`` so persistence can land it in
``crawl_results.options_json``.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from app.adapters.content.scraper.target_safety import reject_unsafe_target_url
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.security.ssrf import make_safe_async_client

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 180
_DEFAULT_URL = "http://webwright:8090"


class WebwrightProvider:
    """Last-resort LLM-driven browser-agent scraper.

    Wraps the Microsoft Webwright sidecar behind the standard
    ContentScraperProtocol. Returns ERROR with a clear reason when the host is
    not allowlisted so the chain's attempt_log records the skip.
    """

    def __init__(
        self,
        *,
        url: str = _DEFAULT_URL,
        host_allowlist: tuple[str, ...] = (),
        max_steps: int = 20,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        min_content_length: int = 400,
        model: str | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._host_allowlist = tuple(h.lower().lstrip(".") for h in host_allowlist)
        self._allow_any = "*" in self._host_allowlist
        self._max_steps = max_steps
        self._timeout_sec = timeout_sec
        self._min_content_length = min_content_length
        self._model = model

    @property
    def provider_name(self) -> str:
        return "webwright"

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        del mobile  # Webwright drives a real desktop Chromium; no mobile toggle.
        started = time.perf_counter()

        unsafe_result = await reject_unsafe_target_url(
            provider="webwright",
            url=url,
            started=started,
            request_id=request_id,
        )
        if unsafe_result is not None:
            return unsafe_result

        if not self._host_in_allowlist(url):
            latency = int((time.perf_counter() - started) * 1000)
            logger.info(
                "webwright_host_not_allowlisted",
                extra={
                    "url": redact_url_for_logging(url),
                    "allowlist_size": len(self._host_allowlist),
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="Webwright: host not in WEBWRIGHT_HOST_ALLOWLIST",
                latency_ms=latency,
                source_url=url,
                endpoint="webwright",
            )

        correlation_id = f"req-{request_id}" if request_id is not None else None

        try:
            payload = await self._post_scrape(url, correlation_id=correlation_id)
        except httpx.TimeoutException:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "webwright_request_timeout",
                extra={
                    "url": redact_url_for_logging(url),
                    "timeout_sec": self._timeout_sec,
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Webwright timeout after {self._timeout_sec}s",
                latency_ms=latency,
                source_url=url,
                endpoint="webwright",
            )
        except httpx.HTTPStatusError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "webwright_http_error",
                extra={
                    "url": redact_url_for_logging(url),
                    "status_code": exc.response.status_code,
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Webwright HTTP {exc.response.status_code}",
                http_status=exc.response.status_code,
                latency_ms=latency,
                source_url=url,
                endpoint="webwright",
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "webwright_fetch_failed",
                extra={
                    "url": redact_url_for_logging(url),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Webwright fetch failed: {exc}",
                latency_ms=latency,
                source_url=url,
                endpoint="webwright",
            )

        latency = int((time.perf_counter() - started) * 1000)
        status = str(payload.get("status") or "").lower()
        body_markdown = (payload.get("body_markdown") or "").strip()
        trajectory_path = payload.get("trajectory_path")
        steps_used = payload.get("steps_used")
        llm_cost_usd = payload.get("llm_cost_usd")

        options_json: dict[str, Any] = {
            "_webwright_trajectory": trajectory_path,
            "_webwright_steps_used": steps_used,
            "_webwright_llm_cost_usd": llm_cost_usd,
            "_webwright_correlation_id": payload.get("correlation_id"),
        }

        if status == "timeout":
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="Webwright: step budget exhausted",
                latency_ms=latency,
                source_url=url,
                endpoint="webwright",
                options_json=options_json,
            )

        if status != "ok" or not body_markdown:
            error_text = (
                payload.get("error_text") or f"Webwright returned status={status!r} with no content"
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Webwright: {error_text}",
                latency_ms=latency,
                source_url=url,
                endpoint="webwright",
                options_json=options_json,
            )

        if len(body_markdown) < self._min_content_length:
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=(f"Webwright: content too short ({len(body_markdown)} chars)"),
                latency_ms=latency,
                source_url=url,
                endpoint="webwright",
                options_json=options_json,
            )

        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None
        if payload.get("title") and metadata is not None and "title" not in metadata:
            metadata = {**metadata, "title": payload["title"]}
        elif payload.get("title") and metadata is None:
            metadata = {"title": payload["title"]}

        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=body_markdown,
            metadata_json=metadata,
            latency_ms=latency,
            source_url=url,
            endpoint="webwright",
            options_json=options_json,
        )

    async def aclose(self) -> None:
        # No persistent client; each request gets a fresh make_safe_async_client.
        return None

    def _host_in_allowlist(self, url: str) -> bool:
        if self._allow_any:
            return True
        if not self._host_allowlist:
            return False
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        for allowed in self._host_allowlist:
            if host == allowed or host.endswith("." + allowed):
                return True
        return False

    async def _post_scrape(self, url: str, *, correlation_id: str | None) -> dict[str, Any]:
        endpoint = f"{self._url}/scrape"
        body = {
            "url": url,
            "max_steps": self._max_steps,
            "timeout_sec": self._timeout_sec,
        }
        if self._model:
            body["model"] = self._model

        headers: dict[str, str] = {"Accept": "application/json"}
        if correlation_id:
            headers["X-Correlation-Id"] = correlation_id

        # Client-side timeout slightly exceeds the sidecar's wall clock so the
        # sidecar gets a chance to return its structured timeout response
        # instead of httpx aborting first.
        async with make_safe_async_client(timeout=self._timeout_sec + 5) as client:
            response = await client.post(endpoint, json=body, headers=headers)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise ValueError(f"Webwright returned non-object payload: {type(data).__name__}")
        return data
