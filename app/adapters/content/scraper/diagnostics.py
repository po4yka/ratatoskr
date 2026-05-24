"""Scraper configuration diagnostics for health endpoints and startup logs."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.config.scraper import profile_retry_budget, profile_timeout_multiplier

if TYPE_CHECKING:
    from app.config import AppConfig


_BROWSER_PROVIDERS = {"cloakbrowser", "playwright", "crawlee"}


def build_scraper_diagnostics(cfg: AppConfig) -> dict[str, Any]:
    scraper_cfg = cfg.scraper
    twitter_cfg = cfg.twitter

    requested_order = (
        [scraper_cfg.force_provider]
        if scraper_cfg.force_provider
        else list(scraper_cfg.provider_order)
    )
    provider_order_effective = [
        name
        for name in requested_order
        if not (name in _BROWSER_PROVIDERS and not scraper_cfg.browser_enabled)
    ]

    profile_multiplier = profile_timeout_multiplier(scraper_cfg.profile)
    firecrawl_self_hosted_enabled = bool(scraper_cfg.firecrawl_self_hosted_enabled)
    firecrawl_cloud_api_key_configured = bool(cfg.firecrawl.api_key)
    # The factory only builds a Firecrawl provider when self-hosted is enabled;
    # a cloud API key alone does not activate the scraper chain provider.
    firecrawl_mode = "self_hosted" if firecrawl_self_hosted_enabled else "disabled"

    providers: dict[str, dict[str, Any]] = {
        "scrapling": {
            "enabled": bool(scraper_cfg.enabled and scraper_cfg.scrapling_enabled),
            "dependency_ready": (
                _module_ready("scrapling")
                and _module_ready("msgspec")
                and _module_ready("trafilatura")
            ),
            "base_timeout_sec": scraper_cfg.scrapling_timeout_sec,
            "effective_timeout_sec": round(
                scraper_cfg.scrapling_timeout_sec * profile_multiplier, 2
            ),
            "stealth_fallback": scraper_cfg.scrapling_stealth_fallback,
            "js_heavy_timeout_multiplier": 0.8,
        },
        "defuddle": {
            "enabled": bool(scraper_cfg.enabled and scraper_cfg.defuddle_enabled),
            "dependency_ready": _module_ready("httpx") and _module_ready("yaml"),
            "api_base_url": scraper_cfg.defuddle_api_base_url,
            "base_timeout_sec": scraper_cfg.defuddle_timeout_sec,
            "effective_timeout_sec": round(
                scraper_cfg.defuddle_timeout_sec * profile_multiplier, 2
            ),
        },
        "firecrawl": {
            "enabled": bool(scraper_cfg.enabled and firecrawl_self_hosted_enabled),
            "dependency_ready": _module_ready("httpx"),
            "mode": firecrawl_mode,
            "self_hosted_enabled": firecrawl_self_hosted_enabled,
            "cloud_api_key_configured": firecrawl_cloud_api_key_configured,
            "cloud_api_key_present_but_unused": (
                firecrawl_cloud_api_key_configured and not firecrawl_self_hosted_enabled
            ),
            "self_hosted_url": scraper_cfg.firecrawl_self_hosted_url,
            "base_timeout_sec": scraper_cfg.firecrawl_timeout_sec,
            "effective_timeout_sec": round(
                scraper_cfg.firecrawl_timeout_sec * profile_multiplier, 2
            ),
            "base_retries": scraper_cfg.firecrawl_max_retries,
            "effective_retries": profile_retry_budget(
                scraper_cfg.firecrawl_max_retries,
                scraper_cfg.profile,
            ),
            "base_wait_for_ms": scraper_cfg.firecrawl_wait_for_ms,
            "js_heavy_wait_for_multiplier": 1.3,
            "js_heavy_wait_for_cap_ms": 10000,
        },
        "cloakbrowser": {
            "enabled": bool(
                scraper_cfg.enabled
                and scraper_cfg.browser_enabled
                and scraper_cfg.cloakbrowser_enabled
            ),
            "dependency_ready": _module_ready("playwright"),
            "endpoint_url": scraper_cfg.cloakbrowser_url,
            "base_timeout_sec": scraper_cfg.cloakbrowser_timeout_sec,
            "effective_timeout_sec": round(
                scraper_cfg.cloakbrowser_timeout_sec * profile_multiplier, 2
            ),
            "humanize": scraper_cfg.cloakbrowser_humanize,
            # Boolean only — proxy URL stays out of /health to avoid leaking
            # credentials embedded in the URL via an unauthenticated endpoint.
            "proxy_configured": bool(scraper_cfg.cloakbrowser_proxy),
            "kind": "browser_sidecar",
        },
        "playwright": {
            "enabled": bool(
                scraper_cfg.enabled
                and scraper_cfg.browser_enabled
                and scraper_cfg.playwright_enabled
            ),
            "dependency_ready": _module_ready("playwright"),
            "headless": scraper_cfg.playwright_headless,
            "base_timeout_sec": scraper_cfg.playwright_timeout_sec,
            "effective_timeout_sec": round(
                scraper_cfg.playwright_timeout_sec * profile_multiplier, 2
            ),
            "js_heavy_timeout_multiplier": 1.25,
        },
        "crawlee": {
            "enabled": bool(
                scraper_cfg.enabled and scraper_cfg.browser_enabled and scraper_cfg.crawlee_enabled
            ),
            "dependency_ready": _module_ready("crawlee"),
            "headless": scraper_cfg.crawlee_headless,
            "base_timeout_sec": scraper_cfg.crawlee_timeout_sec,
            "effective_timeout_sec": round(scraper_cfg.crawlee_timeout_sec * profile_multiplier, 2),
            "base_retries": scraper_cfg.crawlee_max_retries,
            "effective_retries": profile_retry_budget(
                scraper_cfg.crawlee_max_retries, scraper_cfg.profile
            ),
            "js_heavy_timeout_multiplier": 1.25,
        },
        "direct_html": {
            "enabled": bool(scraper_cfg.enabled and scraper_cfg.direct_html_enabled),
            "dependency_ready": _module_ready("httpx"),
            "base_timeout_sec": scraper_cfg.direct_html_timeout_sec,
            "effective_timeout_sec": round(
                scraper_cfg.direct_html_timeout_sec * profile_multiplier, 2
            ),
            "max_response_mb": scraper_cfg.direct_html_max_response_mb,
        },
        "crawl4ai": {
            "enabled": bool(scraper_cfg.enabled and scraper_cfg.crawl4ai_enabled),
            "dependency_ready": _module_ready("httpx"),
            "url": scraper_cfg.crawl4ai_url,
            "token_configured": bool(scraper_cfg.crawl4ai_token),
            "base_timeout_sec": scraper_cfg.crawl4ai_timeout_sec,
            "effective_timeout_sec": round(
                scraper_cfg.crawl4ai_timeout_sec * profile_multiplier, 2
            ),
        },
        "scrapegraph_ai": {
            "enabled": bool(scraper_cfg.enabled and scraper_cfg.scrapegraph_enabled),
            "dependency_ready": _module_ready("scrapegraphai"),
            "model": getattr(cfg.openrouter, "model", ""),
            "base_timeout_sec": scraper_cfg.scrapegraph_timeout_sec,
            "effective_timeout_sec": round(
                scraper_cfg.scrapegraph_timeout_sec * profile_multiplier, 2
            ),
        },
    }

    active_ready = [
        name
        for name, detail in providers.items()
        if detail["enabled"] and detail["dependency_ready"]
    ]
    if not scraper_cfg.enabled:
        status = "disabled"
    elif active_ready:
        status = "healthy"
    else:
        status = "degraded"

    twitter_profile = (
        scraper_cfg.profile
        if twitter_cfg.scraper_profile == "inherit"
        else twitter_cfg.scraper_profile
    )
    twitter_timeout_multiplier = profile_timeout_multiplier(twitter_profile)
    twitter_page_timeout_ms = max(
        1000, int(twitter_cfg.page_timeout_ms * twitter_timeout_multiplier)
    )

    firecrawl_tier_enabled = twitter_cfg.prefer_firecrawl and twitter_cfg.force_tier != "playwright"
    playwright_tier_enabled = (
        twitter_cfg.playwright_enabled and twitter_cfg.force_tier != "firecrawl"
    )

    twitter = {
        "enabled": twitter_cfg.enabled,
        "force_tier": twitter_cfg.force_tier,
        "profile": twitter_profile,
        "profile_source": twitter_cfg.scraper_profile,
        "timeout_multiplier": twitter_timeout_multiplier,
        "page_timeout_ms_effective": twitter_page_timeout_ms,
        "max_concurrent_browsers": twitter_cfg.max_concurrent_browsers,
        "cookies_path": twitter_cfg.cookies_path,
        "cookies_path_exists": Path(twitter_cfg.cookies_path).exists(),
        "firecrawl_tier_enabled": firecrawl_tier_enabled,
        "playwright_tier_enabled": playwright_tier_enabled,
        "playwright_enabled": twitter_cfg.playwright_enabled,
        "prefer_firecrawl": twitter_cfg.prefer_firecrawl,
    }

    return {
        "status": status,
        "enabled": scraper_cfg.enabled,
        "profile": scraper_cfg.profile,
        "browser_enabled": scraper_cfg.browser_enabled,
        "forced_provider": scraper_cfg.force_provider,
        "provider_order_effective": provider_order_effective,
        "min_content_length": scraper_cfg.min_content_length,
        "js_heavy_hosts": list(scraper_cfg.js_heavy_hosts),
        "providers": providers,
        "twitter": twitter,
    }


def _module_ready(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None
