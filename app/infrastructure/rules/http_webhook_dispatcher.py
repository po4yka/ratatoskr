"""HTTP webhook dispatch adapter for automation rules."""

from __future__ import annotations

from app.core.logging_utils import get_logger
from app.domain.services.webhook_service import is_webhook_url_safe
from app.security.ssrf import make_safe_async_client

logger = get_logger(__name__)


class HttpWebhookDispatchAdapter:
    """Dispatch webhook payloads over HTTP."""

    def __init__(self, timeout_seconds: float = 5.0) -> None:
        self._timeout = timeout_seconds

    async def async_dispatch(self, url: str, payload: dict) -> int:
        # Pre-delivery policy check catches stale/unsafe URLs before opening a
        # socket. The safe transport below re-resolves and pins the connection
        # target, closing the DNS-rebinding window between check and connect.
        url_safe, ssrf_error = is_webhook_url_safe(url)
        if not url_safe:
            logger.warning(
                "rule_webhook_blocked_ssrf",
                extra={"url": url, "reason": ssrf_error},
            )
            raise ValueError(f"Webhook URL blocked by SSRF protection: {ssrf_error}")

        async with make_safe_async_client(timeout=self._timeout, follow_redirects=False) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            return response.status_code
