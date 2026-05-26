"""Webwright content enrichment service.

Reusable surface invoked when the summarize loop or a downstream consumer
decides Webwright's heavier browser-agent path is justified — typically
when the cheap scraper providers returned content that is too thin,
behind a paywall, or otherwise insufficient. Callers feed in a URL and
get back enriched content (markdown body) plus telemetry, or None if the
enrichment was skipped (host not allowlisted, sidecar disabled, etc).

This is intentionally separate from the Phase A scraper-chain provider:
the chain decides "did the scrape produce usable text?", while this
service decides "is the post-scrape summary so impoverished that another
LLM-driven scrape pass is worth the cost?". Both share the same sidecar,
and both honor the same WEBWRIGHT_HOST_ALLOWLIST so cost is bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from app.core.logging_utils import get_logger, redact_url_for_logging

if TYPE_CHECKING:
    from app.adapters.webwright.client import WebwrightClient

logger = get_logger(__name__)


@dataclass(frozen=True)
class EnrichmentResult:
    """Outcome of one enrichment attempt."""

    body_markdown: str
    title: str | None
    trajectory_path: str | None
    steps_used: int | None
    llm_cost_usd: float | None


class WebwrightEnricher:
    """Decides when to invoke Webwright for post-scrape content enrichment.

    Callers should treat ``maybe_enrich_url`` as a single shot per request:
    re-invoking it would double cost without changing the page state. The
    caller is responsible for tracking that it was used (e.g. by writing
    an ``LLMCall`` row tagged ``attempt_trigger="webwright_tool"`` after
    re-summarization).
    """

    def __init__(
        self,
        *,
        client: WebwrightClient,
        host_allowlist: tuple[str, ...],
        min_content_length: int = 400,
    ) -> None:
        self._client = client
        self._host_allowlist = tuple(h.lower().lstrip(".") for h in host_allowlist)
        self._allow_any = "*" in self._host_allowlist
        self._min_content_length = min_content_length

    async def maybe_enrich_url(
        self,
        *,
        url: str,
        current_content: str | None,
        correlation_id: str | None = None,
    ) -> EnrichmentResult | None:
        """Try to enrich content via Webwright; return None if skipped.

        Skip conditions (cheap, return None):
            - URL host is not in WEBWRIGHT_HOST_ALLOWLIST
            - Current content is already above the min-content threshold
            - URL is empty
        Failure conditions (return None and log):
            - Sidecar returned non-ok status
            - Sidecar returned thin content
        """

        if not url:
            return None
        if not self._host_in_allowlist(url):
            logger.info(
                "webwright_enrich_skipped_host",
                extra={
                    "url": redact_url_for_logging(url),
                    "cid": correlation_id,
                },
            )
            return None
        if current_content is not None and len(current_content.strip()) >= self._min_content_length:
            logger.info(
                "webwright_enrich_skipped_sufficient_content",
                extra={
                    "url": redact_url_for_logging(url),
                    "cid": correlation_id,
                    "content_len": len(current_content.strip()),
                },
            )
            return None

        task = (
            "Visit the URL below and extract the main article content as "
            "Markdown. Bypass interaction walls (login, expand, accept "
            "cookies) only when credentials are available; otherwise return "
            "what is visible. Respond with a single JSON object "
            "{title, body_markdown, metadata}.\n\nURL: " + url
        )
        host = (urlparse(url).hostname or "").lower()

        result = await self._client.run_task(
            task=task,
            correlation_id=correlation_id,
            allowed_domains=(host,) if host else (),
        )

        if result.status != "ok" or not result.final_answer:
            logger.info(
                "webwright_enrich_failed",
                extra={
                    "url": redact_url_for_logging(url),
                    "cid": correlation_id,
                    "status": result.status,
                    "error": result.error_text,
                },
            )
            return None

        parsed = _parse_webwright_answer(result.final_answer)
        body_raw = parsed.get("body_markdown")
        body = body_raw if isinstance(body_raw, str) else result.final_answer
        body = (body or "").strip()
        if len(body) < self._min_content_length:
            logger.info(
                "webwright_enrich_thin_content",
                extra={
                    "url": redact_url_for_logging(url),
                    "cid": correlation_id,
                    "content_len": len(body),
                },
            )
            return None

        title_raw = parsed.get("title")
        title = title_raw if isinstance(title_raw, str) else None

        return EnrichmentResult(
            body_markdown=body,
            title=title,
            trajectory_path=result.trajectory_path,
            steps_used=result.steps_used,
            llm_cost_usd=result.llm_cost_usd,
        )

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


def _parse_webwright_answer(raw: str) -> dict[str, object]:
    """Be forgiving: Webwright may wrap JSON in markdown fences."""

    import json

    text = (raw or "").strip()
    if text.startswith("```"):
        end = text.find("```", 3)
        if end != -1:
            inner = text[3:end].strip()
            if inner.lower().startswith("json"):
                inner = inner[4:].strip()
            text = inner
    try:
        parsed = json.loads(text)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed
