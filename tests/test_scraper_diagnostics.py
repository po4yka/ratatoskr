"""Tests for scraper diagnostics payload builder."""

from __future__ import annotations

from app.adapters.content.scraper.diagnostics import build_scraper_diagnostics
from app.config import FirecrawlConfig
from app.config.scraper import ScraperConfig
from app.config.twitter import TwitterConfig

from .conftest import make_test_app_config


def test_build_scraper_diagnostics_includes_expected_keys() -> None:
    cfg = make_test_app_config(
        scraper=ScraperConfig(),
        twitter=TwitterConfig(),
    )

    diagnostics = build_scraper_diagnostics(cfg)

    assert diagnostics["status"] in {"healthy", "degraded", "disabled"}
    assert "provider_order_effective" in diagnostics
    assert "providers" in diagnostics
    assert "twitter" in diagnostics
    assert "scrapling" in diagnostics["providers"]
    assert "direct_html" in diagnostics["providers"]
    assert "crawl4ai" in diagnostics["providers"]
    assert "scrapegraph_ai" in diagnostics["providers"]
    assert "cloakbrowser" in diagnostics["providers"]
    assert diagnostics["provider_order_effective"] == [
        "scrapling",
        "direct_pdf",
        "crawl4ai",
        "firecrawl",
        "defuddle",
        "cloakbrowser",
        "playwright",
        "crawlee",
        "direct_html",
        "scrapegraph_ai",
    ]


def test_cloakbrowser_diagnostics_filtered_when_browser_disabled() -> None:
    cfg = make_test_app_config(scraper=ScraperConfig(browser_enabled=False))
    diagnostics = build_scraper_diagnostics(cfg)

    assert "cloakbrowser" not in diagnostics["provider_order_effective"]
    assert diagnostics["providers"]["cloakbrowser"]["enabled"] is False


def test_cloakbrowser_diagnostics_reports_endpoint_url() -> None:
    cfg = make_test_app_config(
        scraper=ScraperConfig(cloakbrowser_url="http://example-cb:9222")
    )
    diagnostics = build_scraper_diagnostics(cfg)

    assert diagnostics["providers"]["cloakbrowser"]["endpoint_url"] == "http://example-cb:9222"
    assert diagnostics["providers"]["cloakbrowser"]["kind"] == "browser_sidecar"


def test_cloakbrowser_diagnostics_exposes_stealth_flags_without_proxy_url() -> None:
    cfg = make_test_app_config(
        scraper=ScraperConfig(
            cloakbrowser_humanize=True,
            cloakbrowser_proxy="socks5://secret:hunter2@10.0.0.5:1080",
        )
    )
    diagnostics = build_scraper_diagnostics(cfg)
    cb = diagnostics["providers"]["cloakbrowser"]

    assert cb["humanize"] is True
    assert cb["proxy_configured"] is True
    # The proxy URL itself must never appear in diagnostics — /health is
    # unauthenticated and the URL embeds credentials.
    for value in cb.values():
        assert "hunter2" not in str(value)
        assert "10.0.0.5" not in str(value)


def test_cloakbrowser_diagnostics_proxy_configured_false_when_empty() -> None:
    cfg = make_test_app_config(
        scraper=ScraperConfig(cloakbrowser_humanize=False, cloakbrowser_proxy="")
    )
    diagnostics = build_scraper_diagnostics(cfg)
    cb = diagnostics["providers"]["cloakbrowser"]

    assert cb["humanize"] is False
    assert cb["proxy_configured"] is False


def test_build_scraper_diagnostics_disabled_state() -> None:
    cfg = make_test_app_config(scraper=ScraperConfig(enabled=False))
    diagnostics = build_scraper_diagnostics(cfg)
    assert diagnostics["status"] == "disabled"


def test_firecrawl_diagnostics_report_disabled_when_only_cloud_key_set() -> None:
    """Cloud Firecrawl was removed; a stale FIRECRAWL_API_KEY must NOT show as enabled."""
    cfg = make_test_app_config(
        scraper=ScraperConfig(firecrawl_self_hosted_enabled=False),
        firecrawl=FirecrawlConfig(api_key="fc-test-cloud-key"),
    )

    diagnostics = build_scraper_diagnostics(cfg)
    firecrawl = diagnostics["providers"]["firecrawl"]

    assert firecrawl["enabled"] is False
    assert firecrawl["mode"] == "disabled"
    assert firecrawl["self_hosted_enabled"] is False
    assert firecrawl["cloud_api_key_present_but_unused"] is True


def test_firecrawl_diagnostics_report_self_hosted_mode_preference() -> None:
    cfg = make_test_app_config(
        scraper=ScraperConfig(firecrawl_self_hosted_enabled=True),
        firecrawl=FirecrawlConfig(api_key="fc-test-cloud-key"),
    )

    diagnostics = build_scraper_diagnostics(cfg)
    firecrawl = diagnostics["providers"]["firecrawl"]

    assert firecrawl["enabled"] is True
    assert firecrawl["mode"] == "self_hosted"
    assert firecrawl["self_hosted_enabled"] is True


def test_firecrawl_diagnostics_report_disabled_without_any_endpoint() -> None:
    cfg = make_test_app_config(
        scraper=ScraperConfig(firecrawl_self_hosted_enabled=False),
        firecrawl=FirecrawlConfig(api_key=""),
    )

    diagnostics = build_scraper_diagnostics(cfg)
    firecrawl = diagnostics["providers"]["firecrawl"]

    assert firecrawl["enabled"] is False
    assert firecrawl["mode"] == "disabled"
    assert firecrawl["cloud_api_key_present_but_unused"] is False
