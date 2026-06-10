"""Tests for WebwrightProvider."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.adapters.content.scraper.webwright_provider import WebwrightProvider


class TestHostAllowlist:
    def test_empty_allowlist_blocks_everything(self):
        provider = WebwrightProvider(host_allowlist=())
        assert provider._host_in_allowlist("https://example.com/a") is False

    def test_exact_host_match(self):
        provider = WebwrightProvider(host_allowlist=("example.com",))
        assert provider._host_in_allowlist("https://example.com/a") is True
        assert provider._host_in_allowlist("https://other.com/a") is False

    def test_subdomain_match(self):
        provider = WebwrightProvider(host_allowlist=("example.com",))
        assert provider._host_in_allowlist("https://www.example.com/a") is True
        assert provider._host_in_allowlist("https://api.example.com/a") is True

    def test_wildcard_allows_any(self):
        provider = WebwrightProvider(host_allowlist=("*",))
        assert provider._host_in_allowlist("https://anything.example/a") is True

    def test_leading_dot_stripped(self):
        provider = WebwrightProvider(host_allowlist=(".example.com",))
        assert provider._host_in_allowlist("https://example.com/a") is True

    def test_case_insensitive(self):
        provider = WebwrightProvider(host_allowlist=("Example.COM",))
        assert provider._host_in_allowlist("https://EXAMPLE.com/a") is True


class TestScrapeMarkdown:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_skips_when_host_not_allowlisted(self):
        provider = WebwrightProvider(host_allowlist=("only-this-host.com",))
        result = await provider.scrape_markdown("https://example.com/article")
        assert result.status == "error"
        assert "WEBWRIGHT_HOST_ALLOWLIST" in (result.error_text or "")
        assert result.endpoint == "webwright"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_successful_scrape(self):
        provider = WebwrightProvider(host_allowlist=("example.com",), min_content_length=10)
        payload = {
            "status": "ok",
            "title": "An Article",
            "body_markdown": "# An Article\n\n" + ("This is the body. " * 20),
            "metadata": {"author": "Jane"},
            "trajectory_path": "/data/webwright/req-123",
            "steps_used": 7,
            "llm_cost_usd": 0.04,
            "correlation_id": "req-123",
        }
        with patch.object(provider, "_post_scrape", new_callable=AsyncMock, return_value=payload):
            result = await provider.scrape_markdown("https://example.com/article", request_id=123)
        assert result.status == "ok"
        assert result.content_markdown is not None
        assert "An Article" in result.content_markdown
        assert (result.metadata_json or {}).get("title") == "An Article"
        assert (result.metadata_json or {}).get("author") == "Jane"
        assert result.endpoint == "webwright"
        assert (result.options_json or {})["_webwright_trajectory"] == "/data/webwright/req-123"
        assert (result.options_json or {})["_webwright_steps_used"] == 7
        assert (result.options_json or {})["_webwright_llm_cost_usd"] == 0.04

    @pytest.mark.asyncio(loop_scope="function")
    async def test_sidecar_timeout_response(self):
        provider = WebwrightProvider(host_allowlist=("example.com",))
        payload = {
            "status": "timeout",
            "trajectory_path": "/data/webwright/req-1",
            "steps_used": 20,
        }
        with patch.object(provider, "_post_scrape", new_callable=AsyncMock, return_value=payload):
            result = await provider.scrape_markdown("https://example.com/x")
        assert result.status == "error"
        assert "step budget" in (result.error_text or "").lower()
        # Trajectory still attached even on timeout so we can debug.
        assert (result.options_json or {})["_webwright_trajectory"] == "/data/webwright/req-1"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_thin_content_returns_error(self):
        provider = WebwrightProvider(host_allowlist=("example.com",), min_content_length=400)
        payload = {"status": "ok", "body_markdown": "Tiny."}
        with patch.object(provider, "_post_scrape", new_callable=AsyncMock, return_value=payload):
            result = await provider.scrape_markdown("https://example.com/x")
        assert result.status == "error"
        assert "too short" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_httpx_timeout_returns_error(self):
        provider = WebwrightProvider(host_allowlist=("example.com",), timeout_sec=5)
        with patch.object(
            provider,
            "_post_scrape",
            new_callable=AsyncMock,
            side_effect=httpx.TimeoutException("boom"),
        ):
            result = await provider.scrape_markdown("https://example.com/x")
        assert result.status == "error"
        assert "timeout" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_http_status_error_propagated(self):
        provider = WebwrightProvider(host_allowlist=("example.com",))
        req = httpx.Request("POST", "http://webwright:8090/scrape")
        resp = httpx.Response(503, request=req)
        exc = httpx.HTTPStatusError("503", request=req, response=resp)
        with patch.object(provider, "_post_scrape", new_callable=AsyncMock, side_effect=exc):
            result = await provider.scrape_markdown("https://example.com/x")
        assert result.status == "error"
        assert result.http_status == 503
        assert "503" in (result.error_text or "")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_generic_exception_returns_error(self):
        provider = WebwrightProvider(host_allowlist=("example.com",))
        with patch.object(
            provider,
            "_post_scrape",
            new_callable=AsyncMock,
            side_effect=RuntimeError("kaboom"),
        ):
            result = await provider.scrape_markdown("https://example.com/x")
        assert result.status == "error"
        assert "kaboom" in (result.error_text or "")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_correlation_id_header_sent(self):
        provider = WebwrightProvider(host_allowlist=("example.com",))
        captured: dict[str, str] = {}

        async def fake_post(url, *, correlation_id):
            captured["url"] = url
            if correlation_id:
                captured["X-Correlation-Id"] = correlation_id
            return {
                "status": "ok",
                "body_markdown": "x" * 500,
                "trajectory_path": "/tmp/t",
            }

        with patch.object(provider, "_post_scrape", side_effect=fake_post):
            await provider.scrape_markdown("https://example.com/x", request_id=42)
        assert captured["X-Correlation-Id"] == "req-42"
        assert captured["url"] == "https://example.com/x"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_status_other_than_ok_returns_error_with_trajectory(self):
        provider = WebwrightProvider(host_allowlist=("example.com",))
        payload = {
            "status": "blocked",
            "trajectory_path": "/tmp/blocked",
            "error_text": "Login wall encountered",
        }
        with patch.object(provider, "_post_scrape", new_callable=AsyncMock, return_value=payload):
            result = await provider.scrape_markdown("https://example.com/x")
        assert result.status == "error"
        assert "Login wall" in (result.error_text or "")
        assert (result.options_json or {})["_webwright_trajectory"] == "/tmp/blocked"
