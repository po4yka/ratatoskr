"""Ordered fallback chain implementing ContentScraperProtocol."""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

from app.adapters.content.quality_filters import best_content_text, detect_low_value_content
from app.adapters.content.scraper.attempt_log import (
    ScraperAttemptEntry,
    ScraperAttemptRecorder,
    serialize_attempt_log,
)
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.observability.metrics import record_scraper_chain_total_latency
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

# Provider tiering for the racing fallback chain. Free providers are pure-HTTP
# scrapes that cost nothing; the paid tier is Firecrawl (managed API or
# self-hosted, both treated as cost-bearing); the browser tier spins up real
# browsers and is the slowest. ``direct_pdf`` is content-specific and always
# runs first when applicable, so it's intentionally left out of any race.
_FREE_TIER_PROVIDERS = frozenset({"scrapling", "defuddle", "direct_html", "crawl4ai"})
_PAID_TIER_PROVIDERS = frozenset({"firecrawl"})
_BROWSER_TIER_PROVIDERS = frozenset(
    {"playwright", "crawlee", "cloakbrowser", "scrapegraph_ai"}
)
_PDF_PROVIDERS = frozenset({"direct_pdf"})


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
        race_enabled: bool = True,
    ) -> None:
        if not providers:
            msg = "ContentScraperChain requires at least one provider"
            raise ValueError(msg)
        self._providers = list(providers)
        self._audit = audit
        self._min_content_length = min_content_length
        self._js_heavy_hosts = js_heavy_hosts
        self._race_enabled = race_enabled

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

    def _grouped_tiers(
        self, providers: list[ContentScraperProtocol]
    ) -> list[tuple[str, list[ContentScraperProtocol]]]:
        """Group providers into ordered (tier_name, providers) buckets.

        Tier order:
          1. ``pdf``     – ``direct_pdf`` runs first when present (PDF-only).
          2. ``free``    – HTTP-only providers raced concurrently.
          3. ``paid``    – Firecrawl, serial.
          4. ``browser`` – browser-driven providers raced concurrently.
          5. ``other``   – any provider not in the above buckets, serial.

        Providers within ``free`` and ``browser`` keep their input order so
        the JS-heavy reorder still applies (browser tier moves first in
        effective_providers and we honor that here).
        """
        by_tier: dict[str, list[ContentScraperProtocol]] = {
            "pdf": [],
            "free": [],
            "paid": [],
            "browser": [],
            "other": [],
        }
        for provider in providers:
            name = provider.provider_name
            if name in _PDF_PROVIDERS:
                by_tier["pdf"].append(provider)
            elif name in _FREE_TIER_PROVIDERS:
                by_tier["free"].append(provider)
            elif name in _PAID_TIER_PROVIDERS:
                by_tier["paid"].append(provider)
            elif name in _BROWSER_TIER_PROVIDERS:
                by_tier["browser"].append(provider)
            else:
                by_tier["other"].append(provider)

        # JS-heavy reorder: if browser tier should run first, swap it ahead.
        # ``_effective_providers`` already put browser providers at the head
        # of the input list, so detect that and reorder the tiers to match.
        if providers and providers[0].provider_name in _BROWSER_TIER_PROVIDERS:
            return [
                ("pdf", by_tier["pdf"]),
                ("browser", by_tier["browser"]),
                ("free", by_tier["free"]),
                ("paid", by_tier["paid"]),
                ("other", by_tier["other"]),
            ]

        return [
            ("pdf", by_tier["pdf"]),
            ("free", by_tier["free"]),
            ("paid", by_tier["paid"]),
            ("browser", by_tier["browser"]),
            ("other", by_tier["other"]),
        ]

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        from app.observability.otel import get_tracer

        _tracer = get_tracer(__name__)
        chain_started = time.monotonic()
        mode = "tiered_race" if self._race_enabled else "serial"

        def _record_outcome(outcome: str) -> None:
            record_scraper_chain_total_latency(
                mode=mode,
                outcome=outcome,
                total_latency_seconds=max(0.0, time.monotonic() - chain_started),
            )

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
            _record_outcome("ssrf_blocked")
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=error_text,
                source_url=url,
                endpoint="chain",
            )

        effective = self._effective_providers(url)
        errors: list[str] = []
        recorder = ScraperAttemptRecorder()

        with _tracer.start_as_current_span(
            "scraper.chain",
            attributes={
                "scraper.url": str(redact_url_for_logging(url)),
                "scraper.mode": mode,
            },
        ) as chain_span:
            if not self._race_enabled:
                winner = await self._run_serial(
                    effective,
                    url,
                    mobile=mobile,
                    request_id=request_id,
                    errors=errors,
                    recorder=recorder,
                    tracer=_tracer,
                    chain_span=chain_span,
                )
            else:
                winner = None
                for tier_name, tier_providers in self._grouped_tiers(effective):
                    if not tier_providers:
                        continue
                    winner = await self._run_tier(
                        tier_name,
                        tier_providers,
                        url,
                        mobile=mobile,
                        request_id=request_id,
                        errors=errors,
                        recorder=recorder,
                        tracer=_tracer,
                        chain_span=chain_span,
                    )
                    if winner is not None:
                        break

            if winner is not None:
                _record_outcome("success")
                return self._attach_attempt_telemetry(winner, recorder)

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
            _record_outcome("empty")
            exhausted = FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"All providers failed: {'; '.join(errors)}",
                source_url=url,
                endpoint="chain",
            )
            return self._attach_attempt_telemetry(exhausted, recorder)

    def _attach_attempt_telemetry(
        self, result: FirecrawlResult, recorder: ScraperAttemptRecorder
    ) -> FirecrawlResult:
        """Stamp the chain's per-provider attempt log onto the returned result.

        The caller persisting `crawl_results` pulls these keys out of
        `options_json` so the DB's `attempt_log` and `winning_provider`
        columns get populated. See content_extractor_requests.persist_crawl_result.
        """
        options = dict(result.options_json or {})
        options["_chain_attempt_log"] = serialize_attempt_log(recorder.entries)
        options["_chain_winning_provider"] = recorder.winner()
        return result.model_copy(update={"options_json": options})

    async def _run_serial(
        self,
        providers: list[ContentScraperProtocol],
        url: str,
        *,
        mobile: bool,
        request_id: int | None,
        errors: list[str],
        recorder: ScraperAttemptRecorder,
        tracer: Any,
        chain_span: Any,
    ) -> FirecrawlResult | None:
        """Original ordered-fallback path; one provider at a time."""
        for provider in providers:
            outcome = await self._attempt_provider(
                provider,
                url,
                mobile=mobile,
                request_id=request_id,
                recorder=recorder,
                tracer=tracer,
            )
            result, error_msg = outcome
            if error_msg is not None:
                errors.append(error_msg)
            if result is not None:
                chain_span.set_attribute("scraper.winner", provider.provider_name)
                chain_span.set_attribute("scraper.attempts", len(errors) + 1)
                self._log_chain_success(provider.provider_name, url, result, request_id, errors)
                return result
        return None

    # Only these tiers race their providers concurrently; the rest run
    # serial-fallback within the tier. ``paid`` only ever has one provider
    # (firecrawl); ``pdf`` and ``other`` keep ordered semantics so that
    # callers passing custom provider sequences (e.g. tests) still see
    # deterministic ordering.
    _RACED_TIERS = frozenset({"free", "browser"})

    async def _run_tier(
        self,
        tier_name: str,
        providers: list[ContentScraperProtocol],
        url: str,
        *,
        mobile: bool,
        request_id: int | None,
        errors: list[str],
        recorder: ScraperAttemptRecorder,
        tracer: Any,
        chain_span: Any,
    ) -> FirecrawlResult | None:
        """Race providers within a tier; first acceptable result wins.

        Single-provider tiers degenerate to a direct ``_attempt_provider``
        call. Multi-provider tiers in ``_RACED_TIERS`` spawn one task per
        provider and cancel losers on first win; other tiers fall back to
        ordered serial execution.
        """
        if tier_name not in self._RACED_TIERS or len(providers) == 1:
            return await self._run_serial(
                providers,
                url,
                mobile=mobile,
                request_id=request_id,
                errors=errors,
                recorder=recorder,
                tracer=tracer,
                chain_span=chain_span,
            )

        logger.info(
            "scraper_tier_race_started",
            extra={
                "tier": tier_name,
                "providers": [p.provider_name for p in providers],
                "url": redact_url_for_logging(url),
                "request_id": request_id,
            },
        )

        tasks: dict[asyncio.Task[Any], ContentScraperProtocol] = {
            asyncio.create_task(
                self._attempt_provider(
                    provider,
                    url,
                    mobile=mobile,
                    request_id=request_id,
                    recorder=recorder,
                    tracer=tracer,
                )
            ): provider
            for provider in providers
        }
        pending = set(tasks)
        winner: FirecrawlResult | None = None
        winner_provider: str | None = None

        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for finished in done:
                    provider = tasks[finished]
                    try:
                        result, error_msg = finished.result()
                    except Exception as exc:  # pragma: no cover — _attempt_provider absorbs
                        error_msg = f"{provider.provider_name}: {exc}"
                        result = None
                    if error_msg is not None:
                        errors.append(error_msg)
                    if result is not None and winner is None:
                        winner = result
                        winner_provider = provider.provider_name
                        break
                if winner is not None:
                    break
        finally:
            for task in pending:
                task.cancel()
            if pending:
                cancelled_losers = [tasks[t].provider_name for t in pending]
                logger.info(
                    "scraper_tier_losers_cancelled",
                    extra={
                        "tier": tier_name,
                        "winner": winner_provider,
                        "cancelled": cancelled_losers,
                        "url": redact_url_for_logging(url),
                        "request_id": request_id,
                    },
                )
                await asyncio.gather(*pending, return_exceptions=True)

        if winner is not None:
            chain_span.set_attribute("scraper.winner", winner_provider or "unknown")
            chain_span.set_attribute("scraper.attempts", len(errors) + 1)
            self._log_chain_success(
                winner_provider or "unknown", url, winner, request_id, errors
            )

        return winner

    async def _attempt_provider(
        self,
        provider: ContentScraperProtocol,
        url: str,
        *,
        mobile: bool,
        request_id: int | None,
        recorder: ScraperAttemptRecorder,
        tracer: Any,
    ) -> tuple[FirecrawlResult | None, str | None]:
        """Run one provider and validate its output.

        Returns ``(result, error_msg)`` where ``result`` is non-None iff the
        provider returned content that survived all chain-side checks
        (non-empty, not an error page, not too short, not low-value).
        """
        name = provider.provider_name
        started = time.monotonic()

        def _record(status: str, error_class: str | None) -> None:
            recorder.record(
                ScraperAttemptEntry(
                    provider=name,
                    status=status,
                    latency_ms=int(max(0.0, time.monotonic() - started) * 1000),
                    error_class=error_class,
                )
            )

        with tracer.start_as_current_span(
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
            except asyncio.CancelledError:
                provider_span.set_attribute("scraper.outcome", "cancelled")
                _record("skipped", "CancelledError")
                raise
            except Exception as exc:
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
                _record("error", type(exc).__name__)
                return None, f"{name}: {exc}"

            has_content = result.status == CallStatus.OK and (
                bool(result.content_markdown and result.content_markdown.strip())
                or bool(result.content_html and result.content_html.strip())
            )

            if has_content:
                text = best_content_text(result)

                if _is_error_page(text):
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
                    _record("error", "error_page")
                    return None, f"{name}: error page detected ({len(text)} chars)"

                if self._min_content_length > 0 and len(text) < self._min_content_length:
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
                    _record("error", "too_short")
                    return None, (
                        f"{name}: content too short"
                        f" ({len(text)} < {self._min_content_length} chars)"
                    )

                quality_issue = (
                    detect_low_value_content(result)
                    if self._min_content_length > 0
                    else None
                )
                if quality_issue is not None:
                    reason = quality_issue["reason"]
                    metrics = quality_issue["metrics"]
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
                    _record("error", f"low_value:{reason}")
                    return None, (
                        f"{name}: low-value content detected"
                        f" ({reason}, chars={metrics['char_length']},"
                        f" words={metrics['word_count']})"
                    )

                provider_span.set_attribute("scraper.outcome", "success")
                _record("success", None)
                return result, None

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
            _record("error", "no_content")
            return None, f"{name}: {result.error_text or 'no content'}"

    def _log_chain_success(
        self,
        provider_name: str,
        url: str,
        result: FirecrawlResult,
        request_id: int | None,
        errors: list[str],
    ) -> None:
        logger.info(
            "scraper_chain_success",
            extra={
                "provider": provider_name,
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
                    "provider": provider_name,
                    "url": redact_url_for_logging(url),
                    "latency_ms": result.latency_ms,
                    "request_id": request_id,
                },
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
