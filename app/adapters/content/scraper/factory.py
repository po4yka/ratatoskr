"""Factory for creating ContentScraperChain from application config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.adapters.content.scraper.chain import ContentScraperChain
from app.adapters.content.scraper.diagnostics import build_scraper_diagnostics
from app.config.scraper import ScraperConfig, profile_retry_budget, profile_timeout_multiplier
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from app.adapters.content.scraper.protocol import ContentScraperProtocol
    from app.config import AppConfig

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScraperProviderDescriptor:
    """Static registration data for one scraper provider."""

    name: str
    enabled: Callable[[AppConfig], bool]
    build: Callable[
        [AppConfig, Callable[[str, str, dict[str, Any]], None] | None],
        ContentScraperProtocol | None,
    ]
    requires_browser: bool = False
    diagnostics_metadata: Mapping[str, Any] | None = None


SCRAPER_PROVIDER_DESCRIPTORS: tuple[ScraperProviderDescriptor, ...] = (
    ScraperProviderDescriptor(
        name="reddit",
        enabled=lambda cfg: bool(cfg.scraper.reddit_enabled),
        build=lambda cfg, _audit: _build_reddit(cfg.scraper),
        diagnostics_metadata={
            "dependency_modules": ("httpx",),
            "kind": "platform_api",
        },
    ),
    ScraperProviderDescriptor(
        name="hn",
        enabled=lambda cfg: bool(cfg.scraper.hn_enabled),
        build=lambda cfg, _audit: _build_hn(cfg.scraper),
        diagnostics_metadata={
            "dependency_modules": ("httpx",),
            "kind": "platform_api",
        },
    ),
    ScraperProviderDescriptor(
        name="scrapling",
        enabled=lambda cfg: bool(cfg.scraper.scrapling_enabled),
        build=lambda cfg, _audit: _build_scrapling(cfg.scraper),
        diagnostics_metadata={
            "dependency_modules": ("scrapling", "trafilatura"),
            "kind": "in_process",
        },
    ),
    ScraperProviderDescriptor(
        name="defuddle",
        enabled=lambda cfg: bool(cfg.scraper.defuddle_enabled),
        build=lambda cfg, _audit: _build_defuddle(cfg.scraper),
        diagnostics_metadata={
            "dependency_modules": ("httpx", "yaml"),
            "kind": "sidecar",
        },
    ),
    ScraperProviderDescriptor(
        name="firecrawl",
        enabled=lambda cfg: bool(cfg.scraper.firecrawl_self_hosted_enabled),
        build=lambda cfg, audit: _build_firecrawl(cfg, audit),
        diagnostics_metadata={
            "dependency_modules": ("httpx",),
            "kind": "self_hosted",
        },
    ),
    ScraperProviderDescriptor(
        name="cloakbrowser",
        enabled=lambda cfg: bool(cfg.scraper.cloakbrowser_enabled),
        build=lambda cfg, audit: _build_cloakbrowser(cfg.scraper, audit),
        requires_browser=True,
        diagnostics_metadata={
            "dependency_modules": ("playwright",),
            "kind": "browser_sidecar",
        },
    ),
    ScraperProviderDescriptor(
        name="playwright",
        enabled=lambda cfg: bool(cfg.scraper.playwright_enabled),
        build=lambda cfg, _audit: _build_playwright(cfg.scraper),
        requires_browser=True,
        diagnostics_metadata={
            "dependency_modules": ("playwright",),
            "kind": "browser",
        },
    ),
    ScraperProviderDescriptor(
        name="crawlee",
        enabled=lambda cfg: bool(cfg.scraper.crawlee_enabled),
        build=lambda cfg, _audit: _build_crawlee(cfg.scraper),
        requires_browser=True,
        diagnostics_metadata={
            "dependency_modules": ("crawlee",),
            "kind": "browser",
        },
    ),
    ScraperProviderDescriptor(
        name="direct_html",
        enabled=lambda cfg: bool(cfg.scraper.direct_html_enabled),
        build=lambda cfg, _audit: _build_direct_html(cfg.scraper),
        diagnostics_metadata={
            "dependency_modules": ("httpx",),
            "kind": "direct",
        },
    ),
    ScraperProviderDescriptor(
        name="direct_pdf",
        enabled=lambda cfg: bool(cfg.scraper.direct_pdf_enabled),
        build=lambda cfg, _audit: _build_direct_pdf(cfg.scraper),
        diagnostics_metadata={
            "dependency_modules": ("fitz",),
            "kind": "direct",
        },
    ),
    ScraperProviderDescriptor(
        name="crawl4ai",
        enabled=lambda cfg: bool(cfg.scraper.crawl4ai_enabled),
        build=lambda cfg, audit: _build_crawl4ai(cfg.scraper, audit),
        diagnostics_metadata={
            "dependency_modules": ("httpx",),
            "kind": "sidecar",
        },
    ),
    ScraperProviderDescriptor(
        name="scrapegraph_ai",
        enabled=lambda cfg: bool(cfg.scraper.scrapegraph_enabled),
        build=lambda cfg, _audit: _build_scrapegraph(cfg),
        diagnostics_metadata={
            "dependency_modules": ("scrapegraphai",),
            "kind": "llm_fallback",
        },
    ),
    ScraperProviderDescriptor(
        name="webwright",
        enabled=lambda cfg: bool(cfg.scraper.webwright_enabled),
        build=lambda cfg, _audit: _build_webwright(cfg),
        diagnostics_metadata={
            "dependency_modules": ("httpx",),
            "kind": "llm_fallback",
        },
    ),
)
SCRAPER_PROVIDER_DESCRIPTOR_BY_NAME: dict[str, ScraperProviderDescriptor] = {
    descriptor.name: descriptor for descriptor in SCRAPER_PROVIDER_DESCRIPTORS
}


class ContentScraperFactory:
    @staticmethod
    def create_from_config(
        cfg: AppConfig,
        audit: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> ContentScraperChain:
        """Build a scraper chain from config, respecting provider_order."""
        scraper_cfg = cfg.scraper

        diagnostics = build_scraper_diagnostics(cfg)
        logger.info("scraper_config_effective", extra={"scraper": diagnostics})

        if not scraper_cfg.enabled:
            from app.adapters.content.scraper.disabled_provider import DisabledScraperProvider

            disabled_provider = DisabledScraperProvider(
                reason="Scraper disabled by SCRAPER_ENABLED=false",
            )
            return ContentScraperChain([disabled_provider], audit=audit)

        providers: list[ContentScraperProtocol] = []

        provider_order = (
            [scraper_cfg.force_provider]
            if scraper_cfg.force_provider
            else list(scraper_cfg.provider_order)
        )

        for name in provider_order:
            descriptor = SCRAPER_PROVIDER_DESCRIPTOR_BY_NAME.get(name)
            if descriptor is None:
                logger.warning("scraper_unknown_provider", extra={"provider": name})
                continue

            if not scraper_cfg.browser_enabled and descriptor.requires_browser:
                logger.info(
                    "scraper_provider_skipped_browser_disabled",
                    extra={"provider": name},
                )
                continue

            if not descriptor.enabled(cfg):
                logger.info(
                    "scraper_provider_skipped_disabled",
                    extra={"provider": name},
                )
                continue

            provider = descriptor.build(cfg, audit)
            if provider is not None:
                providers.append(provider)
                logger.info("scraper_provider_registered", extra={"provider": name})

        if scraper_cfg.force_provider and not providers:
            msg = (
                f"SCRAPER_FORCE_PROVIDER='{scraper_cfg.force_provider}' is unavailable or disabled"
            )
            raise RuntimeError(msg)

        if not providers:
            logger.warning("scraper_no_providers_configured")
            fallback_provider = _build_direct_html(scraper_cfg)
            if fallback_provider is not None:
                providers.append(fallback_provider)
            else:
                from app.adapters.content.scraper.disabled_provider import DisabledScraperProvider

                providers.append(
                    DisabledScraperProvider(
                        reason="No scraper providers are available from configuration"
                    )
                )

        return ContentScraperChain(
            providers,
            audit=audit,
            min_content_length=scraper_cfg.min_content_length,
            js_heavy_hosts=scraper_cfg.js_heavy_hosts,
            race_enabled=scraper_cfg.race_enabled,
        )


def _build_reddit(scraper_cfg: ScraperConfig) -> ContentScraperProtocol | None:
    if not scraper_cfg.reddit_enabled:
        return None

    from app.adapters.content.scraper.reddit_provider import RedditProvider

    timeout_multiplier = profile_timeout_multiplier(scraper_cfg.profile)
    timeout_sec = max(1, round(scraper_cfg.reddit_timeout_sec * timeout_multiplier))
    return RedditProvider(
        timeout_sec=timeout_sec,
        top_comments=scraper_cfg.reddit_top_comments,
        user_agent=scraper_cfg.reddit_user_agent,
    )


def _build_hn(scraper_cfg: ScraperConfig) -> ContentScraperProtocol | None:
    if not scraper_cfg.hn_enabled:
        return None

    from app.adapters.content.scraper.hn_provider import HackerNewsProvider

    timeout_multiplier = profile_timeout_multiplier(scraper_cfg.profile)
    timeout_sec = max(1, round(scraper_cfg.hn_timeout_sec * timeout_multiplier))
    return HackerNewsProvider(
        timeout_sec=timeout_sec,
        top_comments=scraper_cfg.hn_top_comments,
    )


def _build_scrapling(scraper_cfg: ScraperConfig) -> ContentScraperProtocol | None:
    if not scraper_cfg.scrapling_enabled:
        return None
    try:
        from app.adapters.content.scraper.scrapling_provider import ScraplingProvider

        return ScraplingProvider(
            timeout_sec=scraper_cfg.scrapling_timeout_sec,
            stealth_fallback=scraper_cfg.scrapling_stealth_fallback,
            min_content_length=scraper_cfg.min_content_length,
            profile=scraper_cfg.profile,
            js_heavy_hosts=scraper_cfg.js_heavy_hosts,
        )
    except Exception as exc:
        logger.warning(
            "scrapling_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _build_defuddle(scraper_cfg: ScraperConfig) -> ContentScraperProtocol | None:
    if not scraper_cfg.defuddle_enabled:
        return None
    try:
        from app.adapters.content.scraper.defuddle_provider import DefuddleProvider

        timeout_multiplier = profile_timeout_multiplier(scraper_cfg.profile)
        timeout_sec = max(
            1,
            round(scraper_cfg.defuddle_timeout_sec * timeout_multiplier),
        )
        return DefuddleProvider(
            timeout_sec=timeout_sec,
            min_content_length=scraper_cfg.min_content_length,
            api_base_url=scraper_cfg.defuddle_api_base_url,
            api_token=scraper_cfg.defuddle_token,
        )
    except Exception as exc:
        logger.warning(
            "defuddle_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _build_firecrawl(
    cfg: AppConfig,
    audit: Callable[[str, str, dict[str, Any]], None] | None,
) -> ContentScraperProtocol | None:
    """Build Firecrawl provider for self-hosted instance only; cloud is not supported."""
    scraper_cfg = cfg.scraper
    if not scraper_cfg.firecrawl_self_hosted_enabled:
        return None
    try:
        from app.adapters.external.firecrawl.client import FirecrawlClient, FirecrawlClientConfig

        from .firecrawl_provider import FirecrawlProvider

        timeout_multiplier = profile_timeout_multiplier(scraper_cfg.profile)
        profiled_timeout = max(1, round(scraper_cfg.firecrawl_timeout_sec * timeout_multiplier))
        profiled_retries = profile_retry_budget(
            scraper_cfg.firecrawl_max_retries,
            scraper_cfg.profile,
        )

        client_cfg = FirecrawlClientConfig(
            timeout_sec=profiled_timeout,
            max_retries=profiled_retries,
            backoff_base=cfg.firecrawl.retry_initial_delay,
            debug_payloads=cfg.runtime.debug_payloads,
            max_connections=scraper_cfg.firecrawl_max_connections,
            max_keepalive_connections=scraper_cfg.firecrawl_max_keepalive_connections,
            keepalive_expiry=scraper_cfg.firecrawl_keepalive_expiry,
            max_response_size_mb=scraper_cfg.firecrawl_max_response_size_mb,
            max_age_seconds=cfg.firecrawl.max_age_seconds,
            remove_base64_images=cfg.firecrawl.remove_base64_images,
            block_ads=cfg.firecrawl.block_ads,
            skip_tls_verification=cfg.firecrawl.skip_tls_verification,
            include_markdown_format=cfg.firecrawl.include_markdown_format,
            include_html_format=cfg.firecrawl.include_html_format,
            include_links_format=cfg.firecrawl.include_links_format,
            include_summary_format=cfg.firecrawl.include_summary_format,
            include_images_format=cfg.firecrawl.include_images_format,
            enable_screenshot_format=cfg.firecrawl.enable_screenshot_format,
            screenshot_full_page=cfg.firecrawl.screenshot_full_page,
            screenshot_quality=cfg.firecrawl.screenshot_quality,
            screenshot_viewport_width=cfg.firecrawl.screenshot_viewport_width,
            screenshot_viewport_height=cfg.firecrawl.screenshot_viewport_height,
            json_prompt=cfg.firecrawl.json_prompt,
            json_schema=cfg.firecrawl.json_schema or {},
            wait_for_ms=scraper_cfg.firecrawl_wait_for_ms,
        )
        client = FirecrawlClient(
            scraper_cfg.firecrawl_self_hosted_api_key,
            client_cfg,
            audit=audit,
            base_url=scraper_cfg.firecrawl_self_hosted_url,
        )
        return FirecrawlProvider(
            client,
            name="firecrawl_self_hosted",
            wait_for_ms=scraper_cfg.firecrawl_wait_for_ms,
            js_heavy_hosts=scraper_cfg.js_heavy_hosts,
            min_content_length=scraper_cfg.min_content_length,
        )
    except Exception as exc:
        logger.warning(
            "firecrawl_provider_init_failed",
            extra={
                "error": str(exc),
                "error_type": type(exc).__name__,
                "mode": "self_hosted",
            },
        )
        return None


def _build_direct_html(scraper_cfg: ScraperConfig) -> ContentScraperProtocol | None:
    if not scraper_cfg.direct_html_enabled:
        return None

    from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider

    timeout_multiplier = profile_timeout_multiplier(scraper_cfg.profile)
    timeout_sec = max(
        1,
        round(scraper_cfg.direct_html_timeout_sec * timeout_multiplier),
    )

    return DirectHTMLProvider(
        timeout_sec=timeout_sec,
        min_text_length=scraper_cfg.min_content_length,
        max_response_mb=scraper_cfg.direct_html_max_response_mb,
    )


def _build_direct_pdf(scraper_cfg: ScraperConfig) -> ContentScraperProtocol | None:
    if not scraper_cfg.direct_pdf_enabled:
        return None

    try:
        import fitz  # noqa: F401  type: ignore[import-untyped]
    except ImportError:
        return None

    from app.adapters.content.scraper.direct_pdf_provider import DirectPDFProvider

    timeout_multiplier = profile_timeout_multiplier(scraper_cfg.profile)
    timeout_sec = max(
        1,
        round(scraper_cfg.direct_pdf_timeout_sec * timeout_multiplier),
    )

    return DirectPDFProvider(
        timeout_sec=timeout_sec,
        max_pdf_mb=scraper_cfg.direct_pdf_max_size_mb,
        min_text_length=scraper_cfg.min_content_length,
    )


def _build_cloakbrowser(
    scraper_cfg: ScraperConfig,
    audit: Callable[[str, str, dict[str, Any]], None] | None,
) -> ContentScraperProtocol | None:
    if not scraper_cfg.cloakbrowser_enabled:
        return None
    if not scraper_cfg.cloakbrowser_url:
        return None
    try:
        from app.adapters.content.scraper.cloakbrowser_provider import CloakBrowserProvider

        return CloakBrowserProvider(
            endpoint_url=scraper_cfg.cloakbrowser_url,
            timeout_sec=scraper_cfg.cloakbrowser_timeout_sec,
            min_text_length=scraper_cfg.min_content_length,
            profile=scraper_cfg.profile,
            js_heavy_hosts=scraper_cfg.js_heavy_hosts,
            humanize=scraper_cfg.cloakbrowser_humanize,
            proxy=scraper_cfg.cloakbrowser_proxy,
            audit=audit,
        )
    except Exception as exc:
        logger.warning(
            "cloakbrowser_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _build_playwright(scraper_cfg: ScraperConfig) -> ContentScraperProtocol | None:
    if not scraper_cfg.playwright_enabled:
        return None
    try:
        from app.adapters.content.scraper.playwright_provider import PlaywrightProvider

        return PlaywrightProvider(
            timeout_sec=scraper_cfg.playwright_timeout_sec,
            headless=scraper_cfg.playwright_headless,
            min_text_length=scraper_cfg.min_content_length,
            profile=scraper_cfg.profile,
            js_heavy_hosts=scraper_cfg.js_heavy_hosts,
            slim=scraper_cfg.playwright_fingerprint_slim,
        )
    except Exception as exc:
        logger.warning(
            "playwright_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _build_crawlee(scraper_cfg: ScraperConfig) -> ContentScraperProtocol | None:
    if not scraper_cfg.crawlee_enabled:
        return None
    try:
        from app.adapters.content.scraper.crawlee_provider import CrawleeProvider

        profiled_retries = profile_retry_budget(
            scraper_cfg.crawlee_max_retries,
            scraper_cfg.profile,
        )
        return CrawleeProvider(
            timeout_sec=scraper_cfg.crawlee_timeout_sec,
            headless=scraper_cfg.crawlee_headless,
            max_retries=profiled_retries,
            min_content_length=scraper_cfg.min_content_length,
            profile=scraper_cfg.profile,
            js_heavy_hosts=scraper_cfg.js_heavy_hosts,
        )
    except Exception as exc:
        logger.warning(
            "crawlee_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _build_crawl4ai(
    scraper_cfg: ScraperConfig,
    audit: Callable[[str, str, dict[str, Any]], None] | None,
) -> ContentScraperProtocol | None:
    if not scraper_cfg.crawl4ai_enabled:
        return None
    if not scraper_cfg.crawl4ai_url:
        return None
    try:
        from app.adapters.content.scraper.crawl4ai_provider import Crawl4AIProvider

        return Crawl4AIProvider(
            url=scraper_cfg.crawl4ai_url,
            token=scraper_cfg.crawl4ai_token,
            timeout_sec=scraper_cfg.crawl4ai_timeout_sec,
            min_content_length=scraper_cfg.min_content_length,
            profile=scraper_cfg.profile,
            js_heavy_hosts=scraper_cfg.js_heavy_hosts,
            cache_mode=scraper_cfg.crawl4ai_cache_mode,
            audit=audit,
        )
    except Exception as exc:
        logger.warning(
            "crawl4ai_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _build_scrapegraph(cfg: AppConfig) -> ContentScraperProtocol | None:
    scraper_cfg = cfg.scraper
    if not scraper_cfg.scrapegraph_enabled:
        return None
    if not cfg.openrouter.api_key:
        return None
    try:
        from app.adapters.content.scraper.scrapegraph_provider import ScrapeGraphAIProvider

        return ScrapeGraphAIProvider(
            openrouter_api_key=cfg.openrouter.api_key,
            openrouter_model=cfg.openrouter.model,
            timeout_sec=scraper_cfg.scrapegraph_timeout_sec,
            min_content_length=scraper_cfg.min_content_length,
        )
    except Exception as exc:
        logger.warning(
            "scrapegraph_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _build_webwright(cfg: AppConfig) -> ContentScraperProtocol | None:
    scraper_cfg = cfg.scraper
    if not scraper_cfg.webwright_enabled:
        return None
    host_allowlist = tuple(scraper_cfg.webwright_host_allowlist)
    if not host_allowlist:
        logger.info(
            "webwright_provider_skipped_empty_allowlist",
            extra={"reason": "WEBWRIGHT_HOST_ALLOWLIST is empty"},
        )
        return None
    try:
        from app.adapters.content.scraper.webwright_provider import WebwrightProvider

        return WebwrightProvider(
            url=scraper_cfg.webwright_url,
            host_allowlist=host_allowlist,
            max_steps=scraper_cfg.webwright_max_steps,
            timeout_sec=scraper_cfg.webwright_timeout_sec,
            min_content_length=scraper_cfg.min_content_length,
        )
    except Exception as exc:
        logger.warning(
            "webwright_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None
