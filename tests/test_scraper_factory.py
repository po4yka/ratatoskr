"""Tests for ContentScraperFactory."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.adapters.content.scraper.factory import (
    SCRAPER_PROVIDER_DESCRIPTORS,
    ContentScraperFactory,
)
from app.config import FirecrawlConfig
from app.config.scraper import SCRAPER_PROVIDER_TOKENS, ScraperConfig
from tests.conftest import make_test_app_config
from tests.helpers.scraper_helpers import _MockProvider

# ===================================================================
# ContentScraperFactory tests
# ===================================================================


class TestContentScraperFactory:
    """Tests for the factory that builds a scraper chain from config."""

    def test_static_descriptors_cover_config_tokens_and_metadata(self) -> None:
        descriptor_names = {descriptor.name for descriptor in SCRAPER_PROVIDER_DESCRIPTORS}

        assert descriptor_names == SCRAPER_PROVIDER_TOKENS
        assert all(descriptor.diagnostics_metadata for descriptor in SCRAPER_PROVIDER_DESCRIPTORS)
        assert {
            descriptor.name
            for descriptor in SCRAPER_PROVIDER_DESCRIPTORS
            if descriptor.requires_browser
        } == {"cloakbrowser", "playwright", "crawlee"}

    def test_default_config_creates_chain_with_eight_providers_when_firecrawl_disabled(
        self,
    ):
        """Default config (firecrawl self-hosted off) yields an 8-rung chain.

        Firecrawl activates only when FIRECRAWL_SELF_HOSTED_ENABLED=true, and
        scrapegraph_ai activates only when SCRAPER_SCRAPEGRAPH_ENABLED=true.
        """
        cfg = make_test_app_config(scraper=ScraperConfig())

        with (
            patch("app.adapters.content.scraper.factory._build_scrapling") as mock_scrapling,
            patch("app.adapters.content.scraper.factory._build_crawl4ai") as mock_crawl4ai,
            patch("app.adapters.content.scraper.factory._build_firecrawl") as mock_firecrawl,
            patch("app.adapters.content.scraper.factory._build_defuddle") as mock_defuddle,
            patch("app.adapters.content.scraper.factory._build_cloakbrowser") as mock_cloakbrowser,
            patch("app.adapters.content.scraper.factory._build_playwright") as mock_playwright,
            patch("app.adapters.content.scraper.factory._build_crawlee") as mock_crawlee,
            patch("app.adapters.content.scraper.factory._build_direct_html") as mock_direct,
            patch("app.adapters.content.scraper.factory._build_direct_pdf") as mock_direct_pdf,
            patch("app.adapters.content.scraper.factory._build_scrapegraph") as mock_scrapegraph,
        ):
            mock_scrapling.return_value = _MockProvider(name="scrapling")
            mock_direct_pdf.return_value = _MockProvider(name="direct_pdf")
            mock_crawl4ai.return_value = _MockProvider(name="crawl4ai")
            mock_firecrawl.return_value = None  # self-hosted disabled by default
            mock_defuddle.return_value = _MockProvider(name="defuddle")
            mock_cloakbrowser.return_value = _MockProvider(name="cloakbrowser")
            mock_playwright.return_value = _MockProvider(name="playwright")
            mock_crawlee.return_value = _MockProvider(name="crawlee")
            mock_direct.return_value = _MockProvider(name="direct_html")
            mock_scrapegraph.return_value = _MockProvider(name="scrapegraph_ai")

            chain = ContentScraperFactory.create_from_config(cfg)

        names = [p.provider_name for p in chain.providers]
        assert names == [
            "scrapling",
            "direct_pdf",
            "crawl4ai",
            "defuddle",
            "cloakbrowser",
            "playwright",
            "crawlee",
            "direct_html",
        ]
        mock_firecrawl.assert_not_called()
        mock_scrapegraph.assert_not_called()

    def test_default_config_creates_chain_with_nine_providers_when_firecrawl_enabled(
        self,
    ):
        """When FIRECRAWL_SELF_HOSTED_ENABLED=true, firecrawl appears in the chain.

        The factory registers firecrawl (builder key 'firecrawl') but the provider's
        provider_name is 'firecrawl_self_hosted' because _build_firecrawl always sets
        name='firecrawl_self_hosted' on the returned FirecrawlProvider.
        """
        cfg = make_test_app_config(
            scraper=ScraperConfig(
                firecrawl_self_hosted_enabled=True,
                firecrawl_self_hosted_url="http://firecrawl-api:3002",
                firecrawl_self_hosted_api_key="fc-test",
            )
        )

        with (
            patch("app.adapters.content.scraper.factory._build_scrapling") as mock_scrapling,
            patch("app.adapters.content.scraper.factory._build_crawl4ai") as mock_crawl4ai,
            patch("app.adapters.content.scraper.factory._build_firecrawl") as mock_firecrawl,
            patch("app.adapters.content.scraper.factory._build_defuddle") as mock_defuddle,
            patch("app.adapters.content.scraper.factory._build_cloakbrowser") as mock_cloakbrowser,
            patch("app.adapters.content.scraper.factory._build_playwright") as mock_playwright,
            patch("app.adapters.content.scraper.factory._build_crawlee") as mock_crawlee,
            patch("app.adapters.content.scraper.factory._build_direct_html") as mock_direct,
            patch("app.adapters.content.scraper.factory._build_direct_pdf") as mock_direct_pdf,
            patch("app.adapters.content.scraper.factory._build_scrapegraph") as mock_scrapegraph,
        ):
            mock_scrapling.return_value = _MockProvider(name="scrapling")
            mock_direct_pdf.return_value = _MockProvider(name="direct_pdf")
            mock_crawl4ai.return_value = _MockProvider(name="crawl4ai")
            # _build_firecrawl always sets provider_name='firecrawl_self_hosted'
            mock_firecrawl.return_value = _MockProvider(name="firecrawl_self_hosted")
            mock_defuddle.return_value = _MockProvider(name="defuddle")
            mock_cloakbrowser.return_value = _MockProvider(name="cloakbrowser")
            mock_playwright.return_value = _MockProvider(name="playwright")
            mock_crawlee.return_value = _MockProvider(name="crawlee")
            mock_direct.return_value = _MockProvider(name="direct_html")
            mock_scrapegraph.return_value = _MockProvider(name="scrapegraph_ai")

            chain = ContentScraperFactory.create_from_config(cfg)

        names = [p.provider_name for p in chain.providers]
        assert names == [
            "scrapling",
            "direct_pdf",
            "crawl4ai",
            "firecrawl_self_hosted",
            "defuddle",
            "cloakbrowser",
            "playwright",
            "crawlee",
            "direct_html",
        ]
        mock_firecrawl.assert_called_once()
        mock_scrapegraph.assert_not_called()

    def test_scrapling_disabled_skipped(self):
        """When scrapling_enabled=False, the scrapling provider is skipped."""
        scraper_cfg = ScraperConfig(scrapling_enabled=False)
        cfg = make_test_app_config(scraper=scraper_cfg)

        with (
            patch("app.adapters.content.scraper.factory._build_scrapling") as mock_scrapling,
            patch("app.adapters.content.scraper.factory._build_firecrawl") as mock_firecrawl,
            patch("app.adapters.content.scraper.factory._build_playwright") as mock_playwright,
            patch("app.adapters.content.scraper.factory._build_crawlee") as mock_crawlee,
            patch("app.adapters.content.scraper.factory._build_direct_html") as mock_direct,
        ):
            mock_scrapling.return_value = None  # disabled
            mock_firecrawl.return_value = None
            mock_playwright.return_value = _MockProvider(name="playwright")
            mock_crawlee.return_value = _MockProvider(name="crawlee")
            mock_direct.return_value = _MockProvider(name="direct_html")

            chain = ContentScraperFactory.create_from_config(cfg)

        names = [p.provider_name for p in chain.providers]
        assert "scrapling" not in names
        assert "playwright" in names
        assert "crawlee" in names
        assert "direct_html" in names

    def test_playwright_disabled_skipped(self):
        """When playwright_enabled=False, the playwright provider is skipped."""
        scraper_cfg = ScraperConfig(playwright_enabled=False)
        cfg = make_test_app_config(scraper=scraper_cfg)

        with (
            patch("app.adapters.content.scraper.factory._build_scrapling") as mock_scrapling,
            patch("app.adapters.content.scraper.factory._build_firecrawl") as mock_firecrawl,
            patch("app.adapters.content.scraper.factory._build_playwright") as mock_playwright,
            patch("app.adapters.content.scraper.factory._build_crawlee") as mock_crawlee,
            patch("app.adapters.content.scraper.factory._build_direct_html") as mock_direct,
        ):
            mock_scrapling.return_value = _MockProvider(name="scrapling")
            mock_firecrawl.return_value = None
            mock_playwright.return_value = None
            mock_crawlee.return_value = _MockProvider(name="crawlee")
            mock_direct.return_value = _MockProvider(name="direct_html")

            chain = ContentScraperFactory.create_from_config(cfg)

        names = [p.provider_name for p in chain.providers]
        assert "playwright" not in names
        assert "crawlee" in names
        assert "direct_html" in names

    def test_crawlee_disabled_skipped(self):
        """When crawlee_enabled=False, the Crawlee provider is skipped."""
        scraper_cfg = ScraperConfig(crawlee_enabled=False)
        cfg = make_test_app_config(scraper=scraper_cfg)

        with (
            patch("app.adapters.content.scraper.factory._build_scrapling") as mock_scrapling,
            patch("app.adapters.content.scraper.factory._build_firecrawl") as mock_firecrawl,
            patch("app.adapters.content.scraper.factory._build_playwright") as mock_playwright,
            patch("app.adapters.content.scraper.factory._build_crawlee") as mock_crawlee,
            patch("app.adapters.content.scraper.factory._build_direct_html") as mock_direct,
        ):
            mock_scrapling.return_value = _MockProvider(name="scrapling")
            mock_firecrawl.return_value = None
            mock_playwright.return_value = _MockProvider(name="playwright")
            mock_crawlee.return_value = None
            mock_direct.return_value = _MockProvider(name="direct_html")

            chain = ContentScraperFactory.create_from_config(cfg)

        names = [p.provider_name for p in chain.providers]
        assert "crawlee" not in names
        assert "direct_html" in names

    def test_scrapegraph_included_when_enabled_and_ordered(self):
        scraper_cfg = ScraperConfig(
            scrapegraph_enabled=True,
            provider_order=["direct_html", "scrapegraph_ai"],
        )
        cfg = make_test_app_config(scraper=scraper_cfg)

        with (
            patch(
                "app.adapters.content.scraper.factory._build_direct_html",
                return_value=_MockProvider(name="direct_html"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_scrapegraph",
                return_value=_MockProvider(name="scrapegraph_ai"),
            ),
        ):
            chain = ContentScraperFactory.create_from_config(cfg)

        assert [p.provider_name for p in chain.providers] == [
            "direct_html",
            "scrapegraph_ai",
        ]

    def test_firecrawl_self_hosted_enabled_included(self):
        """When firecrawl_self_hosted_enabled=True, firecrawl is in the chain."""
        scraper_cfg = ScraperConfig(
            firecrawl_self_hosted_enabled=True,
            provider_order=["scrapling", "firecrawl", "playwright", "crawlee", "direct_html"],
        )
        cfg = make_test_app_config(scraper=scraper_cfg)

        mock_fc_provider = _MockProvider(name="firecrawl_self_hosted")

        with (
            patch("app.adapters.content.scraper.factory._build_scrapling") as mock_scrapling,
            patch("app.adapters.content.scraper.factory._build_firecrawl") as mock_firecrawl,
            patch("app.adapters.content.scraper.factory._build_playwright") as mock_playwright,
            patch("app.adapters.content.scraper.factory._build_crawlee") as mock_crawlee,
            patch("app.adapters.content.scraper.factory._build_direct_html") as mock_direct,
        ):
            mock_scrapling.return_value = _MockProvider(name="scrapling")
            mock_firecrawl.return_value = mock_fc_provider
            mock_playwright.return_value = _MockProvider(name="playwright")
            mock_crawlee.return_value = _MockProvider(name="crawlee")
            mock_direct.return_value = _MockProvider(name="direct_html")

            chain = ContentScraperFactory.create_from_config(cfg)

        names = [p.provider_name for p in chain.providers]
        assert "firecrawl_self_hosted" in names
        assert "playwright" in names
        assert "crawlee" in names
        assert len(chain.providers) == 5

    def test_firecrawl_cloud_api_key_alone_does_not_include_firecrawl(self):
        """Cloud Firecrawl is removed; even with api_key set, provider is skipped unless self-hosted."""
        scraper_cfg = ScraperConfig(
            provider_order=["firecrawl", "direct_html"],
            firecrawl_self_hosted_enabled=False,
        )
        cfg = make_test_app_config(
            scraper=scraper_cfg,
            firecrawl=FirecrawlConfig(api_key="fc-test-cloud-key"),
        )

        with patch(
            "app.adapters.content.scraper.factory._build_direct_html",
            return_value=_MockProvider(name="direct_html"),
        ):
            chain = ContentScraperFactory.create_from_config(cfg)

        assert [p.provider_name for p in chain.providers] == ["direct_html"]

    def test_firecrawl_without_cloud_key_or_self_hosting_is_skipped(self):
        scraper_cfg = ScraperConfig(provider_order=["firecrawl", "direct_html"])
        cfg = make_test_app_config(
            scraper=scraper_cfg,
            firecrawl=FirecrawlConfig(api_key=""),
        )

        with patch(
            "app.adapters.content.scraper.factory._build_direct_html",
            return_value=_MockProvider(name="direct_html"),
        ):
            chain = ContentScraperFactory.create_from_config(cfg)

        assert [p.provider_name for p in chain.providers] == ["direct_html"]

    def test_firecrawl_self_hosted_preferred_over_cloud_api_key(self):
        scraper_cfg = ScraperConfig(
            firecrawl_self_hosted_enabled=True,
            provider_order=["firecrawl"],
        )
        cfg = make_test_app_config(
            scraper=scraper_cfg,
            firecrawl=FirecrawlConfig(api_key="fc-test-cloud-key"),
        )

        chain = ContentScraperFactory.create_from_config(cfg)

        assert len(chain.providers) == 1
        assert chain.providers[0].provider_name == "firecrawl_self_hosted"

    def test_empty_provider_order_falls_back_to_direct_html(self):
        """When provider_order is empty, the factory falls back to direct_html."""
        scraper_cfg = ScraperConfig(provider_order=[])
        cfg = make_test_app_config(scraper=scraper_cfg)

        chain = ContentScraperFactory.create_from_config(cfg)

        assert len(chain.providers) >= 1
        names = [p.provider_name for p in chain.providers]
        assert "direct_html" in names

    def test_browser_disabled_skips_playwright_crawlee_and_cloakbrowser(self):
        """When browser_enabled=False, all requires_browser providers are skipped."""
        scraper_cfg = ScraperConfig(browser_enabled=False)
        cfg = make_test_app_config(scraper=scraper_cfg)

        with (
            patch("app.adapters.content.scraper.factory._build_scrapling") as mock_scrapling,
            patch("app.adapters.content.scraper.factory._build_firecrawl") as mock_firecrawl,
            patch("app.adapters.content.scraper.factory._build_cloakbrowser") as mock_cloakbrowser,
            patch("app.adapters.content.scraper.factory._build_playwright") as mock_playwright,
            patch("app.adapters.content.scraper.factory._build_crawlee") as mock_crawlee,
            patch("app.adapters.content.scraper.factory._build_direct_html") as mock_direct,
        ):
            mock_scrapling.return_value = _MockProvider(name="scrapling")
            mock_firecrawl.return_value = _MockProvider(name="firecrawl_self_hosted")
            mock_cloakbrowser.return_value = _MockProvider(name="cloakbrowser")
            mock_playwright.return_value = _MockProvider(name="playwright")
            mock_crawlee.return_value = _MockProvider(name="crawlee")
            mock_direct.return_value = _MockProvider(name="direct_html")
            chain = ContentScraperFactory.create_from_config(cfg)

        names = [p.provider_name for p in chain.providers]
        assert "cloakbrowser" not in names
        assert "playwright" not in names
        assert "crawlee" not in names
        assert "scrapling" in names
        assert "direct_html" in names
        mock_cloakbrowser.assert_not_called()
        mock_playwright.assert_not_called()
        mock_crawlee.assert_not_called()

    def test_force_provider_builds_single_provider(self):
        scraper_cfg = ScraperConfig(force_provider="direct_html")
        cfg = make_test_app_config(scraper=scraper_cfg)

        with (
            patch("app.adapters.content.scraper.factory._build_scrapling") as mock_scrapling,
            patch("app.adapters.content.scraper.factory._build_direct_html") as mock_direct,
        ):
            mock_scrapling.return_value = _MockProvider(name="scrapling")
            mock_direct.return_value = _MockProvider(name="direct_html")

            chain = ContentScraperFactory.create_from_config(cfg)

        assert len(chain.providers) == 1
        assert chain.providers[0].provider_name == "direct_html"
        mock_scrapling.assert_not_called()

    def test_force_provider_unavailable_raises_runtime_error(self):
        scraper_cfg = ScraperConfig(force_provider="playwright", browser_enabled=False)
        cfg = make_test_app_config(scraper=scraper_cfg)

        with pytest.raises(RuntimeError, match="SCRAPER_FORCE_PROVIDER='playwright'"):
            ContentScraperFactory.create_from_config(cfg)

    def test_unknown_provider_in_order_is_skipped(self):
        scraper_cfg = ScraperConfig(provider_order=["direct_html"]).model_copy(
            update={"provider_order": ["unknown_provider", "direct_html"]}
        )
        cfg = make_test_app_config(scraper=scraper_cfg)

        with patch(
            "app.adapters.content.scraper.factory._build_direct_html",
            return_value=_MockProvider(name="direct_html"),
        ):
            chain = ContentScraperFactory.create_from_config(cfg)

        assert [p.provider_name for p in chain.providers] == ["direct_html"]

    def test_force_unknown_provider_raises_runtime_error(self):
        scraper_cfg = ScraperConfig(provider_order=["direct_html"]).model_copy(
            update={"force_provider": "unknown_provider"}
        )
        cfg = make_test_app_config(scraper=scraper_cfg)

        with pytest.raises(RuntimeError, match="SCRAPER_FORCE_PROVIDER='unknown_provider'"):
            ContentScraperFactory.create_from_config(cfg)

    def test_scraper_disabled_returns_disabled_provider(self):
        scraper_cfg = ScraperConfig(enabled=False)
        cfg = make_test_app_config(scraper=scraper_cfg)

        chain = ContentScraperFactory.create_from_config(cfg)
        assert len(chain.providers) == 1
        assert chain.providers[0].provider_name == "scraper_disabled"

    def test_audit_callback_forwarded_to_chain(self):
        """The audit callback is passed through to the created chain."""
        scraper_cfg = ScraperConfig(provider_order=["direct_html"])
        cfg = make_test_app_config(scraper=scraper_cfg)
        audit = MagicMock()

        with patch("app.adapters.content.scraper.factory._build_direct_html") as mock_direct:
            mock_direct.return_value = _MockProvider(name="direct_html")
            chain = ContentScraperFactory.create_from_config(cfg, audit=audit)

        assert chain._audit is audit

    def test_defuddle_in_default_chain_when_enabled(self):
        """defuddle is enabled by default and present in the default provider order."""
        cfg = make_test_app_config()
        with (
            patch(
                "app.adapters.content.scraper.factory._build_scrapling",
                return_value=_MockProvider("scrapling"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_crawl4ai",
                return_value=_MockProvider("crawl4ai"),
            ),
            patch("app.adapters.content.scraper.factory._build_firecrawl", return_value=None),
            patch(
                "app.adapters.content.scraper.factory._build_defuddle",
                return_value=_MockProvider("defuddle"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_playwright",
                return_value=_MockProvider("playwright"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_crawlee",
                return_value=_MockProvider("crawlee"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_direct_html",
                return_value=_MockProvider("direct_html"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_scrapegraph",
                return_value=_MockProvider("scrapegraph_ai"),
            ),
        ):
            chain = ContentScraperFactory.create_from_config(cfg)
        names = [p.provider_name for p in chain.providers]
        assert "defuddle" in names

    def test_defuddle_included_when_enabled_and_ordered(self):
        """defuddle appears only when explicitly enabled and present in order."""
        cfg = make_test_app_config(
            scraper=ScraperConfig(
                defuddle_enabled=True,
                provider_order=["scrapling", "defuddle", "direct_html"],
            )
        )
        with (
            patch(
                "app.adapters.content.scraper.factory._build_scrapling",
                return_value=_MockProvider("scrapling"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_defuddle",
                return_value=_MockProvider("defuddle"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_direct_html",
                return_value=_MockProvider("direct_html"),
            ),
        ):
            chain = ContentScraperFactory.create_from_config(cfg)
        names = [p.provider_name for p in chain.providers]
        assert names == ["scrapling", "defuddle", "direct_html"]

    def test_defuddle_disabled_absent_from_chain(self):
        """When _build_defuddle returns None, defuddle is absent."""
        cfg = make_test_app_config()
        with (
            patch(
                "app.adapters.content.scraper.factory._build_scrapling",
                return_value=_MockProvider("scrapling"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_crawl4ai",
                return_value=_MockProvider("crawl4ai"),
            ),
            patch("app.adapters.content.scraper.factory._build_firecrawl", return_value=None),
            patch(
                "app.adapters.content.scraper.factory._build_defuddle",
                return_value=None,
            ),
            patch(
                "app.adapters.content.scraper.factory._build_playwright",
                return_value=_MockProvider("playwright"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_crawlee",
                return_value=_MockProvider("crawlee"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_direct_html",
                return_value=_MockProvider("direct_html"),
            ),
            patch(
                "app.adapters.content.scraper.factory._build_scrapegraph",
                return_value=_MockProvider("scrapegraph_ai"),
            ),
        ):
            chain = ContentScraperFactory.create_from_config(cfg)
        names = [p.provider_name for p in chain.providers]
        assert "defuddle" not in names
