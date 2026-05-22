"""Ordered fallback chain implementing ContentScraperProtocol."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from app.adapters.content.quality_filters import best_content_text, detect_low_value_content
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.security.ssrf import is_url_safe

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.content.scraper.protocol import ContentScraperProtocol

logger = get_logger(__name__)

# Short content containing these patterns is likely an error page, not an article.
_ERROR_PAGE_PATTERNS = re.compile(
    r"\b("
    r"403\s*(forbidden|запрещен|материал\s+снят|доступ\s+запрещ)"
    r"|404\s*(not\s+found|не\s+найден|страница\s+не\s+найдена)"
    r"|401\s*(unauthorized|неавторизован)"
    r"|access\s+denied"
    r"|error\s+\d{3}"
    r"|page\s+not\s+found"
    r"|снят\s+с\s+публикации"
    r"|удалена?\s+автором"
    r"|заблокирован"
    r")\b",
    re.IGNORECASE,
)

# Only flag as error page if content is suspiciously short.
_ERROR_PAGE_MAX_LENGTH = 1500


def _is_error_page(text: str) -> bool:
    """Detect if extracted text is an HTTP error page rather than article content."""
    if not text or len(text) > _ERROR_PAGE_MAX_LENGTH:
        return False
    return bool(_ERROR_PAGE_PATTERNS.search(text))


class ContentScraperChain:
    """Try each provider in order, return the first successful result."""

    def __init__(
        self,
        providers: list[ContentScraperProtocol],
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
        *,
        min_content_length: int = 0,
        js_heavy_hosts: tuple[str, ...] = (),
    ) -> None:
        if not providers:
            msg = "ContentScraperChain requires at least one provider"
            raise ValueError(msg)
        self._providers = list(providers)
        self._audit = audit
        self._min_content_length = min_content_length
        self._js_heavy_hosts = js_heavy_hosts

    @property
    def providers(self) -> list[ContentScraperProtocol]:
        """Read-only view of the provider list."""
        return list(self._providers)

    @property
    def provider_name(self) -> str:
        return "chain"

    def _effective_providers(self, url: str) -> list[ContentScraperProtocol]:
        """Reorder providers for JS-heavy URLs: browser providers first."""
        if not self._js_heavy_hosts:
            return self._providers
        from app.adapters.content.scraper.runtime_tuning import BROWSER_PROVIDERS, is_js_heavy_url

        if not is_js_heavy_url(url, self._js_heavy_hosts):
            return self._providers
        browser = [p for p in self._providers if p.provider_name in BROWSER_PROVIDERS]
        non_browser = [p for p in self._providers if p.provider_name not in BROWSER_PROVIDERS]
        if browser:
            logger.info(
                "scraper_chain_js_heavy_reorder",
                extra={
                    "url": redact_url_for_logging(url),
                    "browser_first": [p.provider_name for p in browser],
                },
            )
        return browser + non_browser

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        from app.observability.otel import get_tracer

        _tracer = get_tracer(__name__)
        errors: list[str] = []
        safe, reason = is_url_safe(url)
        if not safe:
            error_text = f"SSRF blocked URL: {reason}"
            logger.warning(
                "scraper_chain_ssrf_blocked",
                extra={
                    "url": redact_url_for_logging(url),
                    "reason": reason,
                    "request_id": request_id,
                },
            )
            if self._audit:
                self._audit(
                    "ERROR",
                    "scraper_chain_ssrf_blocked",
                    {
                        "url": redact_url_for_logging(url),
                        "reason": reason,
                        "request_id": request_id,
                    },
                )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=error_text,
                source_url=url,
                endpoint="chain",
            )

        with _tracer.start_as_current_span(
            "scraper.chain",
            attributes={"scraper.url": str(redact_url_for_logging(url))},
        ) as chain_span:
            for provider in self._effective_providers(url):
                name = provider.provider_name
                with _tracer.start_as_current_span(
                    f"scraper.{name}",
                    attributes={
                        "scraper.provider": name,
                        "scraper.url": str(redact_url_for_logging(url)),
                    },
                ) as provider_span:
                    try:
                        result = await provider.scrape_markdown(
                            url, mobile=mobile, request_id=request_id
                        )
                    except Exception as exc:
                        error_msg = f"{name}: {exc}"
                        errors.append(error_msg)
                        provider_span.set_attribute("scraper.outcome", "error")
                        provider_span.set_attribute("error.type", type(exc).__name__)
                        logger.warning(
                            "scraper_chain_provider_exception",
                            extra={
                                "provider": name,
                                "url": redact_url_for_logging(url),
                                "error": str(exc),
                                "error_type": type(exc).__name__,
                                "request_id": request_id,
                            },
                        )
                        continue

                    has_content = result.status == CallStatus.OK and (
                        bool(result.content_markdown and result.content_markdown.strip())
                        or bool(result.content_html and result.content_html.strip())
                    )

                    if has_content:
                        text = best_content_text(result)

                        if _is_error_page(text):
                            error_msg = f"{name}: error page detected ({len(text)} chars)"
                            errors.append(error_msg)
                            provider_span.set_attribute("scraper.outcome", "error_page")
                            logger.info(
                                "scraper_chain_error_page",
                                extra={
                                    "provider": name,
                                    "url": redact_url_for_logging(url),
                                    "content_len": len(text),
                                    "request_id": request_id,
                                },
                            )
                            continue

                        if self._min_content_length > 0 and len(text) < self._min_content_length:
                            error_msg = (
                                f"{name}: content too short"
                                f" ({len(text)} < {self._min_content_length} chars)"
                            )
                            errors.append(error_msg)
                            provider_span.set_attribute("scraper.outcome", "too_short")
                            logger.info(
                                "scraper_chain_thin_content",
                                extra={
                                    "provider": name,
                                    "url": redact_url_for_logging(url),
                                    "content_len": len(text),
                                    "threshold": self._min_content_length,
                                    "request_id": request_id,
                                },
                            )
                            continue

                        quality_issue = (
                            detect_low_value_content(result)
                            if self._min_content_length > 0
                            else None
                        )
                        if quality_issue is not None:
                            reason = quality_issue["reason"]
                            metrics = quality_issue["metrics"]
                            error_msg = (
                                f"{name}: low-value content detected"
                                f" ({reason}, chars={metrics['char_length']},"
                                f" words={metrics['word_count']})"
                            )
                            errors.append(error_msg)
                            provider_span.set_attribute("scraper.outcome", "low_value")
                            logger.info(
                                "scraper_chain_low_value_content",
                                extra={
                                    "provider": name,
                                    "url": redact_url_for_logging(url),
                                    "reason": reason,
                                    "metrics": metrics,
                                    "request_id": request_id,
                                },
                            )
                            continue

                    if has_content:
                        provider_span.set_attribute("scraper.outcome", "success")
                        chain_span.set_attribute("scraper.winner", name)
                        chain_span.set_attribute("scraper.attempts", len(errors) + 1)
                        logger.info(
                            "scraper_chain_success",
                            extra={
                                "provider": name,
                                "url": redact_url_for_logging(url),
                                "latency_ms": result.latency_ms,
                                "request_id": request_id,
                                "tried": len(errors) + 1,
                            },
                        )
                        if self._audit:
                            self._audit(
                                "INFO",
                                "scraper_chain_success",
                                {
                                    "provider": name,
                                    "url": redact_url_for_logging(url),
                                    "latency_ms": result.latency_ms,
                                    "request_id": request_id,
                                },
                            )
                        return result

                    error_msg = f"{name}: {result.error_text or 'no content'}"
                    errors.append(error_msg)
                    provider_span.set_attribute("scraper.outcome", "no_content")
                    logger.info(
                        "scraper_chain_provider_failed",
                        extra={
                            "provider": name,
                            "url": redact_url_for_logging(url),
                            "error": result.error_text,
                            "request_id": request_id,
                        },
                    )

            # All providers failed
            chain_span.set_attribute("scraper.attempts", len(errors))
            logger.warning(
                "scraper_chain_exhausted",
                extra={
                    "url": redact_url_for_logging(url),
                    "providers_tried": len(errors),
                    "errors": errors,
                    "request_id": request_id,
                },
            )

            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"All providers failed: {'; '.join(errors)}",
                source_url=url,
                endpoint="chain",
            )

    async def aclose(self) -> None:
        for provider in self._providers:
            try:
                await provider.aclose()
            except Exception as exc:
                logger.debug(
                    "scraper_chain_close_error",
                    extra={
                        "provider": provider.provider_name,
                        "error": str(exc),
                    },
                )
