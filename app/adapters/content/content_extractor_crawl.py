"""Crawl/cache/content-processing helpers for content extraction."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from app.adapters.content.article_media import extract_firecrawl_image_assets
from app.core.call_status import CallStatus

if TYPE_CHECKING:
    import asyncio

    from app.adapters.content.scraper.protocol import ContentScraperProtocol
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.application.ports.requests import LLMCallRecord
    from app.config.settings import AppConfig
    from app.application.ports.message_persistence import (
        MessagePersistencePort as MessagePersistence,
    )

from app.adapters.content.quality_filters import (
    classify_content_quality_llm,
    detect_low_value_content,
    is_gray_zone_for_llm_check,
)
from app.adapters.external.firecrawl.constants import FIRECRAWL_SCRAPE_ENDPOINT
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.async_utils import raise_if_cancelled
from app.core.html_utils import clean_markdown_article_text, html_to_text, normalize_text
from app.core.logging_utils import get_logger
from app.domain.models.request import RequestStatus
from app.observability.failure_observability import (
    REASON_DIRECT_FETCH_FAILED,
    REASON_DNS_RESOLUTION_FAILED,
    REASON_FIRECRAWL_ERROR,
    REASON_FIRECRAWL_LOW_VALUE,
    REASON_SCRAPER_CHAIN_EXHAUSTED,
    persist_request_failure,
)

logger = get_logger(__name__)


def _select_content_text(
    md: str | None,
    html: str | None,
) -> tuple[str, str]:
    """Select raw content text from markdown or HTML. Returns (text, source)."""
    if md and md.strip():
        return clean_markdown_article_text(md), "markdown"
    if html and html.strip():
        return html_to_text(html), "html"
    return "", "none"


def _apply_normalization(content_text: str, cfg: Any) -> str:
    """Apply normalize_text if enable_textacy is configured."""
    try:
        if getattr(cfg.runtime, "enable_textacy", False):
            return normalize_text(content_text)
    except (AttributeError, RuntimeError) as e:
        raise_if_cancelled(e)
        logger.debug("normalization_skipped", extra={"reason": str(e)})
    return content_text


class ContentExtractorCrawlMixin:
    """Scraper cache/processing and HTML salvage behavior."""

    # Explicit host contract: these members are provided by ContentExtractor.
    _audit: Callable[..., None]
    _cache: Any
    _quality_llm_client: Any | None  # optional LLM client for quality classification
    _schedule_crawl_persistence: Callable[..., asyncio.Task[None] | None]
    _sem: Callable[..., Any]
    cfg: AppConfig
    scraper: ContentScraperProtocol
    message_persistence: MessagePersistence
    response_formatter: ResponseFormatter

    async def _extract_or_reuse_content_with_title(
        self,
        message: Any,
        req_id: int,
        url_text: str,
        dedupe_hash: str,
        correlation_id: str | None,
        interaction_id: int | None,
        silent: bool = False,
    ) -> tuple[str, str, str | None, list[str]]:
        """Extract content from the scraper chain or reuse an existing crawl result."""
        existing_crawl = (
            await self.message_persistence.crawl_repo.async_get_crawl_result_by_request(req_id)
        )

        if isinstance(existing_crawl, Mapping):
            existing_crawl = dict(existing_crawl)

        if existing_crawl:
            # Normalize expected payload keys for downstream consumers.
            existing_crawl.setdefault("content_markdown", None)
            existing_crawl.setdefault("content_html", None)

        if existing_crawl and (
            existing_crawl.get("content_markdown") or existing_crawl.get("content_html")
        ):
            return await self._process_existing_crawl_with_title(
                message, existing_crawl, correlation_id, silent
            )
        return await self._perform_new_crawl_with_title(
            message,
            req_id,
            url_text,
            dedupe_hash,
            correlation_id,
            interaction_id,
            silent,
        )

    async def _process_existing_crawl_with_title(
        self,
        message: Any,
        existing_crawl: dict[str, Any],
        correlation_id: str | None,
        silent: bool = False,
    ) -> tuple[str, str, str | None, list[str]]:
        """Process existing crawl result."""
        md = existing_crawl.get("content_markdown")
        html = existing_crawl.get("content_html")

        title = None
        metadata = existing_crawl.get("metadata_json")
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except (json.JSONDecodeError, ValueError):
                metadata = None

        if isinstance(metadata, dict):
            title = metadata.get("title") or metadata.get("og:title")

        crawl_obj = FirecrawlResult(
            status=CallStatus.OK,
            content_markdown=md,
            content_html=html,
            metadata_json=metadata,
            source_url=existing_crawl.get("source_url"),
        )
        images = self._extract_images(crawl_obj)

        content_text, content_source = _select_content_text(md, html)
        if content_source == "html":
            logger.info(
                "html_fallback_used_existing",
                extra={
                    "cid": correlation_id,
                    "reason": "markdown_empty_or_missing",
                    "html_len": len(html or ""),
                    "cleaned_text_len": len(content_text),
                },
            )
        content_text = _apply_normalization(content_text, self.cfg)

        self._audit("INFO", "reuse_crawl_result", {"request_id": None, "cid": correlation_id})

        options_obj = existing_crawl.get("options_json")
        if isinstance(options_obj, str):
            try:
                options_obj = json.loads(options_obj)
            except (json.JSONDecodeError, ValueError):
                options_obj = None

        correlation_from_raw = existing_crawl.get("correlation_id")
        if not correlation_from_raw:
            raw_payload = existing_crawl.get("raw_response_json")
            if isinstance(raw_payload, dict):
                correlation_from_raw = raw_payload.get("cid")
            elif isinstance(raw_payload, str):
                try:
                    parsed_raw = json.loads(raw_payload)
                except (json.JSONDecodeError, ValueError):
                    parsed_raw = None
                if isinstance(parsed_raw, dict):
                    correlation_from_raw = parsed_raw.get("cid")

        latency_val = existing_crawl.get("latency_ms")
        latency_sec = (latency_val / 1000.0) if isinstance(latency_val, int | float) else None

        await self.response_formatter.send_content_reuse_notification(
            message,
            http_status=existing_crawl.get("http_status"),
            crawl_status=existing_crawl.get("status"),
            latency_sec=latency_sec,
            correlation_id=correlation_from_raw,
            options=options_obj,
            silent=silent,
        )

        return content_text, content_source, title, images

    async def _perform_new_crawl_with_title(
        self,
        message: Any,
        req_id: int,
        url_text: str,
        dedupe_hash: str,
        correlation_id: str | None,
        interaction_id: int | None,
        silent: bool = False,
    ) -> tuple[str, str, str | None, list[str]]:
        """Perform new content extraction through the scraper chain."""
        persist_task: asyncio.Task[None] | None = None

        cached_crawl = await self._get_cached_crawl(dedupe_hash, correlation_id)
        if cached_crawl:
            logger.info(
                "firecrawl_cache_hit",
                extra={
                    "cid": correlation_id,
                    "hash": dedupe_hash,
                    "endpoint": cached_crawl.endpoint,
                },
            )
            options_obj = (
                cached_crawl.options_json if isinstance(cached_crawl.options_json, dict) else None
            )
            await self.response_formatter.send_content_reuse_notification(
                message,
                http_status=cached_crawl.http_status,
                crawl_status=cached_crawl.status,
                latency_sec=None,
                correlation_id=cached_crawl.correlation_id,
                options=options_obj,
                silent=silent,
            )
            persist_task = self._schedule_crawl_persistence(req_id, cached_crawl, correlation_id)
            result = await self._process_successful_crawl_with_title(
                message, cached_crawl, correlation_id, silent
            )
            await self._await_persist_task(
                persist_task, correlation_id=correlation_id, event_name="persist_wait_failed"
            )
            return result

        await self.response_formatter.send_firecrawl_start_notification(
            message, url=url_text, silent=silent
        )

        from app.utils.typing_indicator import typing_indicator

        async with typing_indicator(self.response_formatter, message, action="typing"), self._sem():
            crawl = await self.scraper.scrape_markdown(url_text, request_id=req_id)

        quality_issue = await self._apply_low_value_guard(
            crawl=crawl,
            req_id=req_id,
            correlation_id=correlation_id,
        )

        persist_task = self._schedule_crawl_persistence(req_id, crawl, correlation_id)

        logger.debug(
            "crawl_result_debug",
            extra={
                "cid": correlation_id,
                "status": crawl.status,
                "http_status": crawl.http_status,
                "error_text": crawl.error_text,
                "has_markdown": bool(crawl.content_markdown),
                "has_html": bool(crawl.content_html),
                "markdown_len": len(crawl.content_markdown) if crawl.content_markdown else 0,
                "html_len": len(crawl.content_html) if crawl.content_html else 0,
            },
        )

        has_markdown = bool(crawl.content_markdown and crawl.content_markdown.strip())
        has_html = bool(crawl.content_html and crawl.content_html.strip())

        if quality_issue:
            has_markdown = False
            has_html = False

        if crawl.status != CallStatus.OK or not (has_markdown or has_html):
            return await self._recover_or_raise_crawl_failure(
                message,
                req_id=req_id,
                crawl=crawl,
                url_text=url_text,
                dedupe_hash=dedupe_hash,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
                persist_task=persist_task,
                has_markdown=has_markdown,
                has_html=has_html,
                silent=silent,
            )

        return await self._process_successful_crawl_with_title(
            message, crawl, correlation_id, silent
        )

    async def _await_persist_task(
        self,
        persist_task: asyncio.Task[None] | None,
        *,
        correlation_id: str | None,
        event_name: str,
    ) -> None:
        if not persist_task:
            return
        try:
            await persist_task
        except Exception as e:
            raise_if_cancelled(e)
            logger.warning(event_name, extra={"cid": correlation_id, "error": str(e)})

    async def _apply_low_value_guard(
        self,
        *,
        crawl: FirecrawlResult,
        req_id: int,
        correlation_id: str | None,
    ) -> dict[str, Any] | None:
        if crawl.status != CallStatus.OK:
            # Transport-level failure (provider error, SSRF block, DNS
            # failure). Not a content-quality problem: never overwrite the
            # chain's error_text, which carries the real cause. Relabeling
            # these as insufficient_useful_content masked a DNS failure as a
            # paywall during the theatlantic.com triage (request 1450).
            return None

        quality_issue = detect_low_value_content(crawl)
        if not quality_issue:
            return None

        # If in gray zone and LLM quality check enabled, ask LLM to confirm
        content_limits = getattr(self.cfg, "content_limits", None)
        if (
            content_limits is not None
            and content_limits.content_quality_llm_enabled
            and is_gray_zone_for_llm_check(quality_issue["reason"], quality_issue["metrics"])
            and getattr(self, "_quality_llm_client", None) is not None
        ):
            is_stub, llm_result = await classify_content_quality_llm(
                text_preview=quality_issue["preview"],
                metrics=quality_issue["metrics"],
                llm_client=self._quality_llm_client,
                flash_model=self.cfg.openrouter.flash_model,
                flash_fallback_models=self.cfg.openrouter.flash_fallback_models,
                timeout_sec=content_limits.content_quality_llm_timeout_sec,
                confidence_threshold=content_limits.content_quality_llm_confidence_threshold,
                request_id=req_id,
            )
            if llm_result is not None:
                await self._persist_quality_check_llm_call(llm_result, req_id, correlation_id)
            if not is_stub:
                logger.info(
                    "llm_quality_check_override",
                    extra={"cid": correlation_id, "req_id": req_id},
                )
                return None

        metrics = quality_issue["metrics"]
        reason_label = quality_issue["reason"]
        metric_parts = [
            f"chars={metrics['char_length']}",
            f"words={metrics['word_count']}",
            f"unique={metrics['unique_word_count']}",
        ]
        if metrics.get("top_word"):
            metric_parts.append(
                f"top_word={metrics['top_word']}, top_ratio={metrics['top_ratio']:.2f}"
            )
        metric_parts.append(f"overlay_ratio={metrics['overlay_ratio']:.2f}")
        crawl.status = CallStatus.ERROR
        crawl.error_text = f"insufficient_useful_content:{reason_label} ({', '.join(metric_parts)})"
        options_json = dict(crawl.options_json or {})
        options_json["_content_quality"] = {
            "reason": reason_label,
            "char_length": metrics["char_length"],
            "word_count": metrics["word_count"],
            "unique_word_count": metrics["unique_word_count"],
            "overlay_ratio": round(metrics["overlay_ratio"], 3),
            "markdown_len": len(crawl.content_markdown or ""),
            "html_len": len(crawl.content_html or ""),
            "winning_provider": options_json.get("_chain_winning_provider"),
        }
        if metrics.get("top_word"):
            options_json["_content_quality"]["top_word"] = metrics["top_word"]
            options_json["_content_quality"]["top_ratio"] = round(metrics["top_ratio"], 3)
        crawl.options_json = options_json

        if self._audit:
            try:
                audit_payload = {
                    "request_id": req_id,
                    "cid": correlation_id,
                    "reason": reason_label,
                    "char_length": metrics["char_length"],
                    "word_count": metrics["word_count"],
                    "unique_word_count": metrics["unique_word_count"],
                    "overlay_ratio": round(metrics["overlay_ratio"], 3),
                    "markdown_len": len(crawl.content_markdown or ""),
                    "html_len": len(crawl.content_html or ""),
                    "winning_provider": options_json.get("_chain_winning_provider"),
                }
                if metrics.get("top_word"):
                    audit_payload["top_word"] = metrics["top_word"]
                    audit_payload["top_ratio"] = round(metrics["top_ratio"], 3)
                self._audit("WARNING", "firecrawl_low_value_content", audit_payload)
            except Exception as e:
                raise_if_cancelled(e)
                logger.warning("audit_failed", extra={"cid": correlation_id, "error": str(e)})

        logger.warning(
            "firecrawl_low_value_content",
            extra={
                "cid": correlation_id,
                "reason": reason_label,
                **metrics,
            },
        )
        return quality_issue

    async def _persist_quality_check_llm_call(
        self,
        llm_result: Any,
        req_id: int,
        correlation_id: str | None,
    ) -> None:
        """Persist an LLM quality-check call for cost tracking."""
        try:
            record: LLMCallRecord = {
                "request_id": req_id,
                "provider": "openrouter",
                "model": llm_result.model,
                "endpoint": "/quality-check",
                "response_text": llm_result.response_text,
                "tokens_prompt": llm_result.tokens_prompt,
                "tokens_completion": llm_result.tokens_completion,
                "cost_usd": llm_result.cost_usd,
                "latency_ms": llm_result.latency_ms,
                "status": llm_result.status.value if llm_result.status else None,
                "error_text": llm_result.error_text,
            }
            if not bool(
                getattr(
                    getattr(self.cfg, "retention", None),
                    "persist_llm_prompt_response_payloads",
                    True,
                )
            ):
                record["response_text"] = None
                record["response_json"] = {}
                record["request_messages_json"] = []
                record["request_headers_json"] = {}
            await self.message_persistence.llm_repo.async_insert_llm_call(record)
        except Exception:
            logger.warning(
                "quality_check_persist_failed",
                extra={"cid": correlation_id, "req_id": req_id},
            )

    async def _recover_or_raise_crawl_failure(
        self,
        message: Any,
        *,
        req_id: int,
        crawl: FirecrawlResult,
        url_text: str,
        dedupe_hash: str,
        correlation_id: str | None,
        interaction_id: int | None,
        persist_task: asyncio.Task[None] | None,
        has_markdown: bool,
        has_html: bool,
        silent: bool,
    ) -> tuple[str, str, str | None, list[str]]:
        # The scraper chain already tried all providers (including direct HTML).
        # No further salvage attempts needed -- just report the failure.
        await self._await_persist_task(
            persist_task, correlation_id=correlation_id, event_name="persist_wait_failed"
        )
        await self._handle_crawl_error(
            message,
            req_id,
            crawl,
            correlation_id,
            interaction_id,
            has_markdown,
            has_html,
            silent,
        )
        failure_reason = crawl.error_text or "Content extraction failed"
        raise ValueError(f"Content extraction failed: {failure_reason}") from None

    async def _get_cached_crawl(
        self, dedupe_hash: str, correlation_id: str | None
    ) -> FirecrawlResult | None:
        """Fetch a cached Firecrawl result if available and valid."""
        if not self._cache.enabled:
            return None

        from app.adapters.content.content_extractor import URL_ROUTE_VERSION

        cached = await self._cache.get_json("fc", str(URL_ROUTE_VERSION), dedupe_hash)
        if not isinstance(cached, dict):
            return None

        try:
            crawl = FirecrawlResult(**cached)
        except Exception as exc:
            logger.warning(
                "firecrawl_cache_invalid",
                extra={"cid": correlation_id, "error": str(exc), "error_type": type(exc).__name__},
            )
            return None

        if detect_low_value_content(crawl):
            logger.debug(
                "firecrawl_cache_low_value_skipped",
                extra={"cid": correlation_id, "hash": dedupe_hash},
            )
            return None

        return crawl

    async def _write_firecrawl_cache(self, dedupe_hash: str, crawl: FirecrawlResult) -> None:
        """Persist Firecrawl response into Redis cache."""
        if not self._cache.enabled or crawl.status != CallStatus.OK:
            return

        has_markdown = bool(crawl.content_markdown and crawl.content_markdown.strip())
        has_html = bool(crawl.content_html and crawl.content_html.strip())
        if not (has_markdown or has_html):
            return

        payload = crawl.model_dump()
        from app.adapters.content.content_extractor import URL_ROUTE_VERSION

        await self._cache.set_json(
            value=payload,
            ttl_seconds=getattr(self.cfg.redis, "firecrawl_ttl_seconds", 21_600),
            parts=("fc", str(URL_ROUTE_VERSION), dedupe_hash),
        )

    async def _handle_crawl_error(
        self,
        message: Any,
        req_id: int,
        crawl: FirecrawlResult,
        correlation_id: str | None,
        interaction_id: int | None,
        has_markdown: bool,
        has_html: bool,
        silent: bool = False,
    ) -> None:
        """Handle content extraction errors."""
        await self.message_persistence.request_repo.async_update_request_status(
            req_id, RequestStatus.ERROR
        )
        provider = crawl.endpoint or "scraper_chain"
        details = (
            f"🔗 URL: {crawl.source_url or 'unknown'}\n"
            f"🧭 Stage: Content extraction ({provider})\n"
            f"📶 HTTP: {crawl.http_status or 'n/a'}\n"
            f"⚠️ Error: {crawl.error_text or 'unknown'}\n"
            f"🧩 Content received: md:{int(has_markdown)} html:{int(has_html)}"
        )
        await self.response_formatter.send_error_notification(
            message, "firecrawl_error", correlation_id, details=details
        )
        logger.error(
            "firecrawl_error",
            extra={
                "error": crawl.error_text,
                "cid": correlation_id,
                "status": crawl.status,
                "http_status": crawl.http_status,
                "has_markdown": has_markdown,
                "has_html": has_html,
            },
        )
        quality_reason = (
            crawl.error_text
            if crawl.error_text and "insufficient_useful_content" in crawl.error_text
            else None
        )
        is_firecrawl_result = crawl.endpoint == FIRECRAWL_SCRAPE_ENDPOINT
        is_dns_failure = crawl.error_text is not None and crawl.error_text.startswith(
            "dns_resolution_failed"
        )
        if is_dns_failure:
            # Pre-chain DNS failure: transient and retryable; no provider ran.
            # Mislabeling these as SCRAPER_CHAIN_EXHAUSTED hid the real cause
            # during the theatlantic.com triage.
            reason_code = REASON_DNS_RESOLUTION_FAILED
        elif crawl.endpoint == "direct_fetch":
            reason_code = REASON_DIRECT_FETCH_FAILED
        elif is_firecrawl_result and quality_reason is not None:
            reason_code = REASON_FIRECRAWL_LOW_VALUE
        elif is_firecrawl_result:
            reason_code = REASON_FIRECRAWL_ERROR
        else:
            # Chain exhaustion or non-Firecrawl provider failure.
            # Mislabeling these as FIRECRAWL_* hid the real cause during habr.com triage.
            reason_code = REASON_SCRAPER_CHAIN_EXHAUSTED
        await persist_request_failure(
            request_repo=self.message_persistence.request_repo,
            logger=logger,
            request_id=req_id,
            correlation_id=correlation_id,
            stage="extraction",
            component="scraper",
            reason_code=reason_code,
            error=ValueError(crawl.error_text or "Content extraction failed"),
            retryable=True,
            http_status=crawl.http_status,
            latency_ms=crawl.latency_ms,
            source_url=crawl.source_url,
            provider_error_code=getattr(crawl, "firecrawl_error_code", None),
            quality_reason=quality_reason,
            content_signals={
                "has_markdown": has_markdown,
                "has_html": has_html,
            },
        )
        try:
            self._audit(
                "ERROR",
                "firecrawl_error",
                {"request_id": req_id, "cid": correlation_id, "error": crawl.error_text},
            )
        except Exception as e:
            raise_if_cancelled(e)
            logger.debug("audit_failed", extra={"cid": correlation_id, "error": str(e)})

    def _extract_images(self, crawl: FirecrawlResult) -> list[str]:
        """Extract curated image URLs from Firecrawl metadata and markdown."""
        if not bool(getattr(self.cfg.runtime, "aggregation_article_media_enabled", True)):
            return []
        assets, _report = extract_firecrawl_image_assets(crawl)
        return [asset.url for asset in assets if asset.url]

    async def _process_successful_crawl_with_title(
        self,
        message: Any,
        crawl: FirecrawlResult,
        correlation_id: str | None,
        silent: bool = False,
    ) -> tuple[str, str, str | None, list[str]]:
        """Process successful Firecrawl result."""
        title = None
        if crawl.metadata_json:
            title = crawl.metadata_json.get("title") or crawl.metadata_json.get("og:title")

        images = self._extract_images(crawl)

        excerpt_len = (len(crawl.content_markdown) if crawl.content_markdown else 0) or (
            len(crawl.content_html) if crawl.content_html else 0
        )
        latency_sec = (crawl.latency_ms or 0) / 1000.0
        await self.response_formatter.send_firecrawl_success_notification(
            message,
            excerpt_len,
            latency_sec,
            http_status=crawl.http_status,
            crawl_status=crawl.status,
            correlation_id=crawl.correlation_id,
            endpoint=crawl.endpoint,
            options=crawl.options_json,
            silent=silent,
        )

        content_text, content_source = _select_content_text(
            crawl.content_markdown, crawl.content_html
        )
        if content_source == "html":
            logger.info(
                "html_fallback_used",
                extra={
                    "cid": correlation_id,
                    "reason": "markdown_empty_or_missing",
                    "html_len": len(crawl.content_html or ""),
                    "cleaned_text_len": len(content_text),
                },
            )
            await self.response_formatter.send_html_fallback_notification(
                message, len(content_text), silent=silent
            )
        elif content_source == "none":
            logger.error(
                "no_content_available",
                extra={
                    "cid": correlation_id,
                    "markdown_len": len(crawl.content_markdown) if crawl.content_markdown else 0,
                    "html_len": len(crawl.content_html) if crawl.content_html else 0,
                },
            )
        content_text = _apply_normalization(content_text, self.cfg)

        return content_text, content_source, title, images
