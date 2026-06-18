"""Shared target URL safety guard for scraper providers."""

from __future__ import annotations

import time

from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.security.ssrf import is_dns_failure_reason, is_url_safe_async

logger = get_logger(__name__)


async def reject_unsafe_target_url(
    *,
    provider: str,
    url: str,
    started: float,
    request_id: int | None = None,
) -> FirecrawlResult | None:
    """Return an error result when *url* is not safe for provider-side fetching."""
    safe, reason = await is_url_safe_async(url)
    if safe:
        return None

    latency = int((time.perf_counter() - started) * 1000)
    dns_failure = is_dns_failure_reason(reason)
    event = f"{provider}_dns_failed" if dns_failure else f"{provider}_ssrf_blocked"
    error_text = (
        f"{provider} DNS resolution failed: {reason}"
        if dns_failure
        else f"{provider} SSRF blocked URL: {reason}"
    )
    logger.warning(
        event,
        extra={
            "url": redact_url_for_logging(url),
            "reason": reason,
            "request_id": request_id,
        },
    )
    return FirecrawlResult(
        status=CallStatus.ERROR,
        error_text=error_text,
        latency_ms=latency,
        source_url=url,
        endpoint=provider,
    )
