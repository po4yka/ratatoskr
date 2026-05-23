"""Factory for creating ContentScraperChain from application config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.adapters.content.scraper.chain import ContentScraperChain
from app.adapters.content.scraper.diagnostics import build_scraper_diagnostics
from app.config.scraper import profile_retry_budget, profile_timeout_multiplier
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
        name="scrapling",
        enabled=lambda cfg: bool(getattr(cfg.scraper, "scrapling_enabled", True)),
        build=lambda cfg, _audit: _build_scrapling(cfg.scraper),
        diagnostics_metadata={
            "dependency_modules": ("scrapling", "trafilatura"),
            "kind": "in_process",
        },
    ),
    ScraperProviderDescriptor(
        name="defuddle",
        enabled=lambda cfg: bool(getattr(cfg.scraper, "defuddle_enabled", True)),
        build=lambda cfg, _audit: _build_defuddle(cfg.scraper),
        diagnostics_metadata={
            "dependency_modules": ("httpx", "yaml"),
            "kind": "sidecar",
        },
    ),
    ScraperProviderDescriptor(
        name="firecrawl",
        enabled=lambda cfg: bool(getattr(cfg.scraper, "firecrawl_self_hosted_enabled", False)),
        build=lambda cfg, audit: _build_firecrawl(cfg, audit),
        diagnostics_metadata={
            "dependency_modules": ("httpx",),
            "kind": "self_hosted",
        },
    ),
    ScraperProviderDescriptor(
        name="cloakbrowser",
        enabled=lambda cfg: bool(getattr(cfg.scraper, "cloakbrowser_enabled", True)),
        build=lambda cfg, audit: _build_cloakbrowser(cfg.scraper, audit),
        requires_browser=True,
        diagnostics_metadata={
            "dependency_modules": ("playwright",),
            "kind": "browser_sidecar",
        },
    ),
    ScraperProviderDescriptor(
        name="playwright",
        enabled=lambda cfg: bool(getattr(cfg.scraper, "playwright_enabled", True)),
        build=lambda cfg, _audit: _build_playwright(cfg.scraper),
        requires_browser=True,
        diagnostics_metadata={
            "dependency_modules": ("playwright",),
            "kind": "browser",
        },
    ),
    ScraperProviderDescriptor(
        name="crawlee",
        enabled=lambda cfg: bool(getattr(cfg.scraper, "crawlee_enabled", True)),
        build=lambda cfg, _audit: _build_crawlee(cfg.scraper),
        requires_browser=True,
        diagnostics_metadata={
            "dependency_modules": ("crawlee",),
            "kind": "browser",
        },
    ),
    ScraperProviderDescriptor(
        name="direct_html",
        enabled=lambda cfg: bool(getattr(cfg.scraper, "direct_html_enabled", True)),
        build=lambda cfg, _audit: _build_direct_html(cfg.scraper),
        diagnostics_metadata={
            "dependency_modules": ("httpx",),
            "kind": "direct",
        },
    ),
    ScraperProviderDescriptor(
        name="direct_pdf",
        enabled=lambda cfg: bool(getattr(cfg.scraper, "direct_pdf_enabled", True)),
        build=lambda cfg, _audit: _build_direct_pdf(cfg.scraper),
        diagnostics_metadata={
            "dependency_modules": ("fitz",),
            "kind": "direct",
        },
    ),
    ScraperProviderDescriptor(
        name="crawl4ai",
        enabled=lambda cfg: bool(getattr(cfg.scraper, "crawl4ai_enabled", True)),
        build=lambda cfg, audit: _build_crawl4ai(cfg.scraper, audit),
        diagnostics_metadata={
            "dependency_modules": ("httpx",),
            "kind": "sidecar",
        },
    ),
    ScraperProviderDescriptor(
        name="scrapegraph_ai",
        enabled=lambda cfg: bool(getattr(cfg.scraper, "scrapegraph_enabled", True)),
        build=lambda cfg, _audit: _build_scrapegraph(cfg),
        diagnostics_metadata={
            "dependency_modules": ("scrapegraphai",),
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
            min_content_length=getattr(scraper_cfg, "min_content_length", 400),
            js_heavy_hosts=getattr(scraper_cfg, "js_heavy_hosts", ()),
        )


def _build_scrapling(scraper_cfg: object) -> ContentScraperProtocol | None:
    if not getattr(scraper_cfg, "scrapling_enabled", True):
        return None
    try:
        from app.adapters.content.scraper.scrapling_provider import ScraplingProvider

        return ScraplingProvider(
            timeout_sec=getattr(scraper_cfg, "scrapling_timeout_sec", 30),
            stealth_fallback=getattr(scraper_cfg, "scrapling_stealth_fallback", True),
            min_content_length=getattr(scraper_cfg, "min_content_length", 400),
            profile=getattr(scraper_cfg, "profile", "balanced"),
            js_heavy_hosts=getattr(scraper_cfg, "js_heavy_hosts", ()),
        )
    except Exception as exc:
        logger.warning(
            "scrapling_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _build_defuddle(scraper_cfg: object) -> ContentScraperProtocol | None:
    if not getattr(scraper_cfg, "defuddle_enabled", True):
        return None
    try:
        from app.adapters.content.scraper.defuddle_provider import DefuddleProvider

        timeout_multiplier = profile_timeout_multiplier(getattr(scraper_cfg, "profile", "balanced"))
        timeout_sec = max(
            1,
            round(getattr(scraper_cfg, "defuddle_timeout_sec", 20) * timeout_multiplier),
        )
        return DefuddleProvider(
            timeout_sec=timeout_sec,
            min_content_length=getattr(scraper_cfg, "min_content_length", 400),
            api_base_url=getattr(scraper_cfg, "defuddle_api_base_url", "https://defuddle.md"),
            api_token=getattr(scraper_cfg, "defuddle_token", ""),
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
    if not getattr(scraper_cfg, "firecrawl_self_hosted_enabled", False):
        return None
    try:
        from app.adapters.external.firecrawl.client import FirecrawlClient, FirecrawlClientConfig

        from .firecrawl_provider import FirecrawlProvider

        profile = getattr(scraper_cfg, "profile", "balanced")
        timeout_multiplier = profile_timeout_multiplier(profile)
        profiled_timeout = max(
            1, round(getattr(scraper_cfg, "firecrawl_timeout_sec", 90) * timeout_multiplier)
        )
        profiled_retries = profile_retry_budget(
            getattr(scraper_cfg, "firecrawl_max_retries", 3),
            profile,
        )

        client_cfg = FirecrawlClientConfig(
            timeout_sec=profiled_timeout,
            max_retries=profiled_retries,
            backoff_base=cfg.firecrawl.retry_initial_delay,
            debug_payloads=cfg.runtime.debug_payloads,
            max_connections=getattr(scraper_cfg, "firecrawl_max_connections", 10),
            max_keepalive_connections=getattr(
                scraper_cfg, "firecrawl_max_keepalive_connections", 5
            ),
            keepalive_expiry=getattr(scraper_cfg, "firecrawl_keepalive_expiry", 30.0),
            max_response_size_mb=getattr(scraper_cfg, "firecrawl_max_response_size_mb", 50),
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
            wait_for_ms=getattr(scraper_cfg, "firecrawl_wait_for_ms", 3000),
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
            wait_for_ms=getattr(scraper_cfg, "firecrawl_wait_for_ms", 3000),
            js_heavy_hosts=getattr(scraper_cfg, "js_heavy_hosts", ()),
            min_content_length=getattr(scraper_cfg, "min_content_length", 400),
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


def _build_direct_html(scraper_cfg: object) -> ContentScraperProtocol | None:
    if not getattr(scraper_cfg, "direct_html_enabled", True):
        return None

    from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider

    timeout_multiplier = profile_timeout_multiplier(getattr(scraper_cfg, "profile", "balanced"))
    timeout_sec = max(
        1,
        round(getattr(scraper_cfg, "direct_html_timeout_sec", 30) * timeout_multiplier),
    )

    return DirectHTMLProvider(
        timeout_sec=timeout_sec,
        min_text_length=getattr(scraper_cfg, "min_content_length", 400),
        max_response_mb=getattr(scraper_cfg, "direct_html_max_response_mb", 10),
    )


def _build_direct_pdf(scraper_cfg: object) -> ContentScraperProtocol | None:
    if not getattr(scraper_cfg, "direct_pdf_enabled", True):
        return None

    try:
        import fitz  # noqa: F401  type: ignore[import-untyped]
    except ImportError:
        return None

    from app.adapters.content.scraper.direct_pdf_provider import DirectPDFProvider

    timeout_multiplier = profile_timeout_multiplier(getattr(scraper_cfg, "profile", "balanced"))
    timeout_sec = max(
        1,
        round(getattr(scraper_cfg, "direct_pdf_timeout_sec", 60) * timeout_multiplier),
    )

    return DirectPDFProvider(
        timeout_sec=timeout_sec,
        max_pdf_mb=getattr(scraper_cfg, "direct_pdf_max_size_mb", 20),
        min_text_length=getattr(scraper_cfg, "min_content_length", 400),
    )


def _build_cloakbrowser(
    scraper_cfg: object,
    audit: Callable[[str, str, dict[str, Any]], None] | None,
) -> ContentScraperProtocol | None:
    if not getattr(scraper_cfg, "cloakbrowser_enabled", True):
        return None
    endpoint_url = getattr(scraper_cfg, "cloakbrowser_url", "")
    if not endpoint_url:
        return None
    try:
        from app.adapters.content.scraper.cloakbrowser_provider import CloakBrowserProvider

        return CloakBrowserProvider(
            endpoint_url=endpoint_url,
            timeout_sec=getattr(scraper_cfg, "cloakbrowser_timeout_sec", 60),
            min_text_length=getattr(scraper_cfg, "min_content_length", 400),
            profile=getattr(scraper_cfg, "profile", "balanced"),
            js_heavy_hosts=getattr(scraper_cfg, "js_heavy_hosts", ()),
            humanize=getattr(scraper_cfg, "cloakbrowser_humanize", True),
            proxy=getattr(scraper_cfg, "cloakbrowser_proxy", ""),
            audit=audit,
        )
    except Exception as exc:
        logger.warning(
            "cloakbrowser_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _build_playwright(scraper_cfg: object) -> ContentScraperProtocol | None:
    if not getattr(scraper_cfg, "playwright_enabled", True):
        return None
    try:
        from app.adapters.content.scraper.playwright_provider import PlaywrightProvider

        return PlaywrightProvider(
            timeout_sec=getattr(scraper_cfg, "playwright_timeout_sec", 30),
            headless=getattr(scraper_cfg, "playwright_headless", True),
            min_text_length=getattr(scraper_cfg, "min_content_length", 400),
            profile=getattr(scraper_cfg, "profile", "balanced"),
            js_heavy_hosts=getattr(scraper_cfg, "js_heavy_hosts", ()),
            slim=getattr(scraper_cfg, "playwright_fingerprint_slim", False),
        )
    except Exception as exc:
        logger.warning(
            "playwright_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _build_crawlee(scraper_cfg: object) -> ContentScraperProtocol | None:
    if not getattr(scraper_cfg, "crawlee_enabled", True):
        return None
    try:
        from app.adapters.content.scraper.crawlee_provider import CrawleeProvider

        profile = getattr(scraper_cfg, "profile", "balanced")
        profiled_retries = profile_retry_budget(
            getattr(scraper_cfg, "crawlee_max_retries", 2),
            profile,
        )
        return CrawleeProvider(
            timeout_sec=getattr(scraper_cfg, "crawlee_timeout_sec", 45),
            headless=getattr(scraper_cfg, "crawlee_headless", True),
            max_retries=profiled_retries,
            min_content_length=getattr(scraper_cfg, "min_content_length", 400),
            profile=profile,
            js_heavy_hosts=getattr(scraper_cfg, "js_heavy_hosts", ()),
        )
    except Exception as exc:
        logger.warning(
            "crawlee_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None


def _build_crawl4ai(
    scraper_cfg: object,
    audit: Callable[[str, str, dict[str, Any]], None] | None,
) -> ContentScraperProtocol | None:
    if not getattr(scraper_cfg, "crawl4ai_enabled", True):
        return None
    crawl4ai_url = getattr(scraper_cfg, "crawl4ai_url", "")
    if not crawl4ai_url:
        return None
    try:
        from app.adapters.content.scraper.crawl4ai_provider import Crawl4AIProvider

        return Crawl4AIProvider(
            url=crawl4ai_url,
            token=getattr(scraper_cfg, "crawl4ai_token", ""),
            timeout_sec=getattr(scraper_cfg, "crawl4ai_timeout_sec", 60),
            min_content_length=getattr(scraper_cfg, "min_content_length", 400),
            profile=getattr(scraper_cfg, "profile", "balanced"),
            js_heavy_hosts=getattr(scraper_cfg, "js_heavy_hosts", ()),
            cache_mode=getattr(scraper_cfg, "crawl4ai_cache_mode", "BYPASS"),
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
    if not getattr(scraper_cfg, "scrapegraph_enabled", True):
        return None
    openrouter_api_key = getattr(cfg.openrouter, "api_key", "")
    if not openrouter_api_key:
        return None
    try:
        from app.adapters.content.scraper.scrapegraph_provider import ScrapeGraphAIProvider

        return ScrapeGraphAIProvider(
            openrouter_api_key=openrouter_api_key,
            openrouter_model=getattr(cfg.openrouter, "model", ""),
            timeout_sec=getattr(scraper_cfg, "scrapegraph_timeout_sec", 90),
            min_content_length=getattr(scraper_cfg, "min_content_length", 400),
        )
    except Exception as exc:
        logger.warning(
            "scrapegraph_provider_init_failed",
            extra={"error": str(exc), "error_type": type(exc).__name__},
        )
        return None
