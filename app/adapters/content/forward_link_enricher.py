"""Fold the content of links embedded in a forwarded post into its summary prompt.

A forwarded channel post often hyperlinks words pointing to source articles. This
enricher fetches the full content of those referenced articles and appends them to
the forward's LLM prompt as labelled sections, so the single post summary reflects
the referenced material. Modelled on ``SearchContextEnricher``: best-effort,
config-tuned, fails soft -- a scrape failure never blocks the forward summary.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.core.urls.forward_link_extraction import extract_forward_urls

if TYPE_CHECKING:
    from app.adapters.content.content_extractor import ContentExtractor
    from app.config import AppConfig

logger = get_logger(__name__)

# Keep the enriched prompt under ForwardSummarizer._MAX_FORWARD_CONTENT_CHARS
# (45_000) with headroom, so the summarizer's blunt tail-truncation never fires
# mid-article.
_FORWARD_ENRICHMENT_BUDGET = 44_000
_TRUNCATION_MARKER = " […]"
# Below this many usable body chars an article section is not worth including.
_MIN_USABLE_BODY_CHARS = 200


class ForwardLinkEnricher:
    """Append the full content of a forwarded post's embedded links to its prompt."""

    def __init__(self, *, cfg: AppConfig, content_extractor: ContentExtractor) -> None:
        self._cfg = cfg
        self._content_extractor = content_extractor

    async def enrich(
        self,
        *,
        message: Any,
        base_prompt: str,
        post_text: str,
        correlation_id: str | None,
    ) -> str:
        """Return ``base_prompt`` enriched with referenced-article sections.

        Returns ``base_prompt`` unchanged when there are no links, the post is
        already too long, or anything goes wrong -- the forward must still
        summarize from its own text.
        """
        try:
            return await self._enrich(
                message=message,
                base_prompt=base_prompt,
                post_text=post_text,
                correlation_id=correlation_id,
            )
        except Exception as exc:  # pragma: no cover - defensive: never block the forward
            raise_if_cancelled(exc)
            logger.warning(
                "forward_link_enrichment_error",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            return base_prompt

    async def _enrich(
        self,
        *,
        message: Any,
        base_prompt: str,
        post_text: str,
        correlation_id: str | None,
    ) -> str:
        urls = extract_forward_urls(message, post_text, correlation_id=correlation_id)
        if not urls:
            return base_prompt

        max_links = self._cfg.runtime.forward_link_max_links
        if len(urls) > max_links:
            logger.info(
                "forward_link_enrichment_capped",
                extra={"cid": correlation_id, "found": len(urls), "cap": max_links},
            )
            urls = urls[:max_links]

        budget = _FORWARD_ENRICHMENT_BUDGET - len(base_prompt)
        if budget <= 0:
            logger.debug(
                "forward_link_enrichment_skipped",
                extra={"cid": correlation_id, "reason": "post_too_long"},
            )
            return base_prompt

        fetched = await self._fetch_all(urls, correlation_id)
        successful = [item for item in fetched if item is not None]
        if not successful:
            logger.info(
                "forward_link_enrichment_done",
                extra={
                    "cid": correlation_id,
                    "urls_found": len(urls),
                    "urls_fetched": 0,
                    "urls_failed": len(urls),
                    "total_chars": len(base_prompt),
                },
            )
            return base_prompt

        sections = self._compose_sections(successful, budget)
        if not sections:
            return base_prompt

        enriched = base_prompt + "\n\n" + "\n\n".join(sections)
        logger.info(
            "forward_link_enrichment_done",
            extra={
                "cid": correlation_id,
                "urls_found": len(urls),
                "urls_fetched": len(sections),
                "urls_failed": len(urls) - len(successful),
                "total_chars": len(enriched),
            },
        )
        return enriched

    async def _fetch_all(
        self, urls: list[str], correlation_id: str | None
    ) -> list[tuple[str, str, dict[str, Any]] | None]:
        """Scrape every URL concurrently; failed/timed-out fetches become ``None``."""
        timeout = self._cfg.runtime.forward_link_per_url_timeout_sec

        async def _one(url: str) -> tuple[str, str, dict[str, Any]] | None:
            try:
                async with asyncio.timeout(timeout):
                    # request_id=None: a sub-link failure must NOT mark the
                    # forward's own request as failed.
                    (
                        content_text,
                        _source,
                        metadata,
                    ) = await self._content_extractor.extract_content_pure(
                        url, correlation_id, request_id=None
                    )
                return (url, content_text, metadata if isinstance(metadata, dict) else {})
            except TimeoutError:
                logger.warning(
                    "forward_link_fetch_failed",
                    extra={
                        "cid": correlation_id,
                        "url": redact_url_for_logging(url),
                        "error": "timeout",
                    },
                )
                return None
            except Exception as exc:
                raise_if_cancelled(exc)
                logger.warning(
                    "forward_link_fetch_failed",
                    extra={
                        "cid": correlation_id,
                        "url": redact_url_for_logging(url),
                        "error": str(exc),
                    },
                )
                return None

        return list(await asyncio.gather(*(_one(url) for url in urls)))

    def _compose_sections(
        self,
        successful: list[tuple[str, str, dict[str, Any]]],
        budget: int,
    ) -> list[str]:
        """Build labelled ``## Referenced article`` sections within the char budget."""
        per_article_cap = self._cfg.runtime.forward_link_per_article_chars
        sections: list[str] = []
        remaining = budget
        total = len(successful)

        for index, (url, content_text, metadata) in enumerate(successful):
            articles_left = total - index
            # Fair share of the remaining budget, bounded by the per-article cap.
            cap = min(per_article_cap, max(0, remaining // articles_left))
            body = (content_text or "").strip()
            if not body or cap <= 0:
                break

            title = self._title_for(url, metadata)
            header = f"## Referenced article: {title}\n{url}\n\n"
            body_budget = cap - len(header)
            if body_budget < _MIN_USABLE_BODY_CHARS:
                break

            if len(body) > body_budget:
                body = body[:body_budget].rstrip() + _TRUNCATION_MARKER
            section = header + body
            sections.append(section)
            remaining -= len(section) + 2  # +2 for the "\n\n" join separator

        return sections

    @staticmethod
    def _title_for(url: str, metadata: dict[str, Any]) -> str:
        """Best-effort article title from extractor metadata; fall back to the host."""
        if isinstance(metadata, dict):
            for key in ("title",):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            for nested_key in ("firecrawl_metadata", "normalized_source_document"):
                nested = metadata.get(nested_key)
                if isinstance(nested, dict):
                    value = nested.get("title")
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return urlparse(url).netloc or url
