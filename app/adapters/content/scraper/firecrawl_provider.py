"""Firecrawl-based content extraction provider (wraps existing FirecrawlClient)."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from app.adapters.content.quality_filters import best_content_text
from app.adapters.content.scraper.runtime_tuning import tuned_firecrawl_wait_for_ms
from app.adapters.content.scraper.target_safety import reject_unsafe_target_url
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.external.firecrawl.client import FirecrawlClient

logger = get_logger(__name__)


class FirecrawlProvider:
    """Scraper provider backed by FirecrawlClient (cloud or self-hosted)."""

    def __init__(
        self,
        client: FirecrawlClient,
        *,
        name: str = "firecrawl",
        wait_for_ms: int = 3000,
        js_heavy_hosts: tuple[str, ...] = (),
        min_content_length: int = 400,
    ) -> None:
        self._client = client
        self._name = name
        self._wait_for_ms = wait_for_ms
        self._js_heavy_hosts = js_heavy_hosts
        self._min_content_length = min_content_length

    @property
    def provider_name(self) -> str:
        return self._name

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        started = time.perf_counter()
        unsafe_result = await reject_unsafe_target_url(
            provider=self._name,
            url=url,
            started=started,
            request_id=request_id,
        )
        if unsafe_result is not None:
            return unsafe_result

        wait_for_ms = tuned_firecrawl_wait_for_ms(
            base_wait_for_ms=self._wait_for_ms,
            url=url,
            js_heavy_hosts=self._js_heavy_hosts,
        )
        result = await self._client.scrape_markdown(
            url,
            mobile=mobile,
            request_id=request_id,
            wait_for_ms_override=wait_for_ms,
        )

        if result.status == CallStatus.OK:
            text = best_content_text(result)

            if len(text) < self._min_content_length:
                logger.info(
                    "firecrawl_thin_content",
                    extra={
                        "url": url,
                        "content_len": len(text),
                        "threshold": self._min_content_length,
                        "request_id": request_id,
                    },
                )
                return FirecrawlResult(
                    status=CallStatus.ERROR,
                    error_text=(
                        f"Firecrawl: content too short"
                        f" ({len(text)} < {self._min_content_length} chars)"
                    ),
                    content_html=result.content_html,
                    latency_ms=result.latency_ms,
                    source_url=result.source_url,
                    endpoint=result.endpoint,
                )

        return result

    async def aclose(self) -> None:
        await self._client.aclose()
