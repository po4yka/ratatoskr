"""Tests for DefuddleProvider."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.adapters.content.scraper.defuddle_provider import (
    DefuddleProvider,
    _parse_frontmatter,
    _parse_yaml_safe,
)


class TestParseFrontmatter:
    def test_valid_frontmatter_extracted(self):
        raw = "---\ntitle: Hello\nauthor: Jane\n---\n# Article\nBody."
        meta, md = _parse_frontmatter(raw)
        assert meta == {"title": "Hello", "author": "Jane"}
        assert "# Article" in md

    def test_no_frontmatter_returns_raw(self):
        raw = "# Just Markdown"
        meta, md = _parse_frontmatter(raw)
        assert meta == {}
        assert md == raw

    def test_unclosed_frontmatter_returns_raw(self):
        raw = "---\ntitle: Broken\nno closing"
        meta, md = _parse_frontmatter(raw)
        assert meta == {}
        assert md == raw

    def test_empty_frontmatter_block(self):
        raw = "---\n---\n# Body"
        meta, md = _parse_frontmatter(raw)
        assert meta == {}
        assert "# Body" in md


class TestParseYamlSafe:
    def test_valid_yaml(self):
        assert _parse_yaml_safe("title: Hello") == {"title": "Hello"}

    def test_empty_returns_empty_dict(self):
        assert _parse_yaml_safe("") == {}

    def test_malformed_returns_empty_dict(self):
        assert _parse_yaml_safe("title: [unclosed") == {}

    def test_list_yaml_returns_empty_dict(self):
        assert _parse_yaml_safe("- a\n- b") == {}


class TestDefuddleProvider:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_successful_scrape(self):
        body = "---\ntitle: Article\nauthor: Jane\n---\n" + "A" * 500
        provider = DefuddleProvider(timeout_sec=5, min_content_length=100)
        with patch.object(provider, "_fetch_raw", new_callable=AsyncMock, return_value=body):
            result = await provider.scrape_markdown("https://example.com")
        assert result.status == "ok"
        assert result.metadata_json == {"title": "Article", "author": "Jane"}
        assert result.endpoint == "defuddle"
        assert result.source_url == "https://example.com"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_thin_content_returns_error(self):
        body = "---\ntitle: Short\n---\nTiny."
        provider = DefuddleProvider(timeout_sec=5, min_content_length=400)
        with patch.object(provider, "_fetch_raw", new_callable=AsyncMock, return_value=body):
            result = await provider.scrape_markdown("https://example.com")
        assert result.status == "error"
        assert "too short" in result.error_text.lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_empty_response_returns_error(self):
        provider = DefuddleProvider(timeout_sec=5)
        with patch.object(provider, "_fetch_raw", new_callable=AsyncMock, return_value=""):
            result = await provider.scrape_markdown("https://example.com")
        assert result.status == "error"
        assert "empty" in result.error_text.lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_timeout_returns_error(self):
        provider = DefuddleProvider(timeout_sec=1)
        with patch.object(
            provider, "_fetch_raw", new_callable=AsyncMock, side_effect=TimeoutError()
        ):
            result = await provider.scrape_markdown("https://example.com")
        assert result.status == "error"
        assert "timeout" in result.error_text.lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_http_status_error_returns_error(self):
        provider = DefuddleProvider(timeout_sec=5)
        req = httpx.Request("GET", "https://defuddle.md/https://example.com")
        resp = httpx.Response(404, request=req)
        exc = httpx.HTTPStatusError("404", request=req, response=resp)
        with patch.object(provider, "_fetch_raw", new_callable=AsyncMock, side_effect=exc):
            result = await provider.scrape_markdown("https://example.com")
        assert result.status == "error"
        assert "404" in result.error_text
        assert result.http_status == 404

    @pytest.mark.asyncio(loop_scope="function")
    async def test_http_401_logs_redacted_authorization_header(
        self, caplog: pytest.LogCaptureFixture
    ):
        token = "defuddle-secret-token-12345"
        provider = DefuddleProvider(timeout_sec=5, api_token=token)
        req = httpx.Request("GET", "http://defuddle-api:3003/https://example.com")
        resp = httpx.Response(401, request=req)
        exc = httpx.HTTPStatusError("401 Unauthorized", request=req, response=resp)

        with caplog.at_level(logging.WARNING, logger="app.adapters.content.scraper.defuddle_provider"):
            with patch.object(provider, "_fetch_raw", new_callable=AsyncMock, side_effect=exc):
                result = await provider.scrape_markdown("https://example.com")

        rendered = "\n".join(record.getMessage() + str(record.__dict__) for record in caplog.records)
        assert token not in rendered
        assert token not in (result.error_text or "")
        record = next(record for record in caplog.records if record.message == "defuddle_http_error")
        request_headers = record.__dict__["request_headers"]
        assert request_headers["Authorization"] == "[REDACTED]"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_generic_exception_returns_error(self):
        provider = DefuddleProvider(timeout_sec=5)
        with patch.object(
            provider,
            "_fetch_raw",
            new_callable=AsyncMock,
            side_effect=RuntimeError("connection refused"),
        ):
            result = await provider.scrape_markdown("https://example.com")
        assert result.status == "error"
        assert "connection refused" in result.error_text.lower()

    def test_default_api_base_url_is_self_hosted(self):
        """Default URL is the self-hosted Docker Compose service, not defuddle.md."""
        p = DefuddleProvider()
        assert p._api_base_url == "http://defuddle-api:3003"

    def test_api_base_url_trailing_slash_stripped(self):
        p = DefuddleProvider(api_base_url="https://self-hosted.internal/")
        assert not p._api_base_url.endswith("/")

    def test_cloud_url_logs_deprecation_warning(self, caplog):
        """Pointing at https://defuddle.md logs a deprecation warning."""
        import logging

        with caplog.at_level(logging.WARNING):
            DefuddleProvider(api_base_url="https://defuddle.md")

        warning_events = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("defuddle_provider_cloud_url_deprecated" in msg for msg in warning_events)

    @pytest.mark.asyncio(loop_scope="function")
    async def test_no_frontmatter_still_ok_when_long_enough(self):
        body = "# Plain\n" + "B" * 500
        provider = DefuddleProvider(timeout_sec=5, min_content_length=100)
        with patch.object(provider, "_fetch_raw", new_callable=AsyncMock, return_value=body):
            result = await provider.scrape_markdown("https://example.com")
        assert result.status == "ok"
        assert result.metadata_json is None
