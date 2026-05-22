"""Tests for scraper configuration parsing and validation."""

from __future__ import annotations

import pytest

from app.config.scraper import ScraperConfig
from app.config.settings import raise_on_deprecated_scraper_env_vars


def test_provider_order_accepts_csv() -> None:
    cfg = ScraperConfig(provider_order="scrapling,firecrawl,playwright,direct_html")  # type: ignore[arg-type]
    assert cfg.provider_order == ["scrapling", "firecrawl", "playwright", "direct_html"]


def test_default_provider_order_has_nine_entries() -> None:
    cfg = ScraperConfig()

    assert cfg.provider_order == [
        "scrapling",
        "direct_pdf",
        "crawl4ai",
        "firecrawl",
        "defuddle",
        "playwright",
        "crawlee",
        "direct_html",
        "scrapegraph_ai",
    ]


def test_defuddle_enabled_by_default() -> None:
    cfg = ScraperConfig()
    assert cfg.defuddle_enabled is True


def test_private_network_url_override_disabled_by_default() -> None:
    cfg = ScraperConfig()
    assert cfg.allow_private_network_urls is False


def test_private_network_url_override_accepts_env_alias() -> None:
    cfg = ScraperConfig(SCRAPER_ALLOW_PRIVATE_NETWORK_URLS=True)
    assert cfg.allow_private_network_urls is True


def test_new_provider_tokens_in_token_set() -> None:
    from app.config.scraper import SCRAPER_PROVIDER_TOKENS

    assert "crawl4ai" in SCRAPER_PROVIDER_TOKENS
    assert "scrapegraph_ai" in SCRAPER_PROVIDER_TOKENS


def test_provider_order_accepts_json_array_string() -> None:
    cfg = ScraperConfig(provider_order='["scrapling", "crawlee", "direct_html"]')  # type: ignore[arg-type]
    assert cfg.provider_order == ["scrapling", "crawlee", "direct_html"]


def test_provider_order_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError, match="Unknown scraper provider"):
        ScraperConfig(provider_order=["scrapling", "unknown"])


def test_provider_order_rejects_duplicates() -> None:
    with pytest.raises(ValueError, match="Duplicate scraper provider"):
        ScraperConfig(provider_order="scrapling,scrapling,direct_html")  # type: ignore[arg-type]


def test_force_provider_validation() -> None:
    cfg = ScraperConfig(force_provider="playwright")
    assert cfg.force_provider == "playwright"

    with pytest.raises(ValueError, match="SCRAPER_FORCE_PROVIDER"):
        ScraperConfig(force_provider="bad-provider")


def test_deprecated_scraper_env_vars_fail_fast(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SCRAPLING_ENABLED", "true")

    with pytest.raises(RuntimeError, match="SCRAPLING_ENABLED -> SCRAPER_SCRAPLING_ENABLED"):
        raise_on_deprecated_scraper_env_vars()


def test_deprecated_scraper_env_vars_detected_in_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SCRAPLING_ENABLED", raising=False)
    (tmp_path / ".env").write_text("SCRAPLING_TIMEOUT_SEC=30\n", encoding="utf-8")

    with pytest.raises(
        RuntimeError, match="SCRAPLING_TIMEOUT_SEC -> SCRAPER_SCRAPLING_TIMEOUT_SEC"
    ):
        raise_on_deprecated_scraper_env_vars()
