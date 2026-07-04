"""Tests for Crawl4AIProvider."""

from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.adapters.content.scraper.crawl4ai_provider import Crawl4AIProvider


def _make_crawl_response(
    *,
    success: bool = True,
    markdown: str | dict | None = "A" * 500,
    error: str = "unknown",
) -> dict:
    """Build a minimal Crawl4AI /crawl response payload."""
    result: dict = {"success": success}
    if success:
        result["markdown"] = markdown
        result["metadata"] = {"title": "Test Article"}
    else:
        result["error"] = error
    return {"results": [result]}


def _make_httpx_response(
    payload: dict | None = None,
    status_code: int = 200,
    *,
    content: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    """Build a real httpx.Response, since the provider streams the real object's
    `.headers`, `.raise_for_status()`, and `.aiter_bytes()` rather than a bare mock.
    """
    body = content if content is not None else json.dumps(payload or {}).encode()
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    return httpx.Response(
        status_code,
        content=body,
        headers=headers,
        request=httpx.Request("POST", "http://crawl4ai:11235/crawl"),
    )


class _StreamContextManager:
    """Mimics the async context manager returned by `httpx.AsyncClient.stream()`."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def __aenter__(self) -> httpx.Response:
        return self._response

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def _mock_stream(mock_client: AsyncMock, response: httpx.Response) -> None:
    """Wire `mock_client.stream(...)` to return an async context manager yielding
    `response`, matching `async with client.stream(...) as resp:` in the provider.
    """
    mock_client.stream = MagicMock(return_value=_StreamContextManager(response))


class TestCrawl4AIProvider:
    @pytest.mark.asyncio(loop_scope="function")
    async def test_successful_response_with_markdown_string(self):
        """Successful /crawl response with markdown as string -> OK result."""
        payload = _make_crawl_response(markdown="A" * 500)
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=5)

        mock_client = AsyncMock()
        _mock_stream(mock_client, _make_httpx_response(payload))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.endpoint == "crawl4ai"
        assert result.content_markdown is not None
        assert len(result.content_markdown) >= 400

    @pytest.mark.asyncio(loop_scope="function")
    async def test_successful_response_with_markdown_dict_raw_markdown(self):
        """Successful response with markdown as dict (raw_markdown key) -> extracts content."""
        md_dict = {"raw_markdown": "B" * 500, "fit_markdown": ""}
        payload = _make_crawl_response(markdown=md_dict)
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=5)

        mock_client = AsyncMock()
        _mock_stream(mock_client, _make_httpx_response(payload))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.endpoint == "crawl4ai"
        assert "B" in (result.content_markdown or "")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_successful_response_with_markdown_dict_fit_markdown(self):
        """fit_markdown takes precedence over raw_markdown in dict shape."""
        md_dict = {"fit_markdown": "C" * 500, "raw_markdown": "D" * 500}
        payload = _make_crawl_response(markdown=md_dict)
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=5)

        mock_client = AsyncMock()
        _mock_stream(mock_client, _make_httpx_response(payload))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "ok"
        assert result.content_markdown is not None
        assert result.content_markdown.startswith("C")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_empty_markdown_returns_error(self):
        """Empty/short markdown -> ERROR with min_content_length message."""
        payload = _make_crawl_response(markdown="tiny")
        provider = Crawl4AIProvider(
            url="http://crawl4ai:11235", timeout_sec=5, min_content_length=400
        )

        mock_client = AsyncMock()
        _mock_stream(mock_client, _make_httpx_response(payload))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "crawl4ai"
        assert (
            "too short" in (result.error_text or "").lower()
            or "content" in (result.error_text or "").lower()
        )

    @pytest.mark.asyncio(loop_scope="function")
    async def test_http_500_returns_error(self):
        """HTTP 500 from Crawl4AI -> ERROR result with status code."""
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=5)

        mock_client = AsyncMock()
        req = httpx.Request("POST", "http://crawl4ai:11235/crawl")
        resp = httpx.Response(500, request=req)
        _mock_stream(mock_client, resp)

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "crawl4ai"
        assert "500" in (result.error_text or "")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_timeout_returns_error(self):
        """Timeout -> ERROR result."""
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=1)

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(side_effect=TimeoutError("timed out"))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "crawl4ai"
        assert "timeout" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_aclose_closes_underlying_client(self):
        """aclose() closes the underlying httpx client."""
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=5)

        mock_client = AsyncMock(spec=httpx.AsyncClient)
        provider._client = mock_client

        await provider.aclose()

        mock_client.aclose.assert_awaited_once()
        assert provider._client is None

    @pytest.mark.asyncio(loop_scope="function")
    async def test_result_not_success_returns_error(self):
        """When result has success=false, returns ERROR with reported error detail."""
        payload = _make_crawl_response(success=False, error="fetch failed")
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=5)

        mock_client = AsyncMock()
        _mock_stream(mock_client, _make_httpx_response(payload))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "crawl4ai"
        assert "fetch failed" in (result.error_text or "")

    @pytest.mark.asyncio(loop_scope="function")
    async def test_empty_results_array_returns_error(self):
        """Empty results array -> ERROR."""
        payload = {"results": []}
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=5)

        mock_client = AsyncMock()
        _mock_stream(mock_client, _make_httpx_response(payload))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "crawl4ai"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_authorization_header_set_when_token_provided(self):
        """Authorization header is sent when a token is configured."""
        payload = _make_crawl_response(markdown="A" * 500)
        provider = Crawl4AIProvider(
            url="http://crawl4ai:11235", token="secret-token", timeout_sec=5
        )

        mock_client = AsyncMock()
        _mock_stream(mock_client, _make_httpx_response(payload))

        with patch.object(provider, "_get_client", return_value=mock_client):
            await provider.scrape_markdown("https://example.com")

        call_kwargs = mock_client.stream.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert headers.get("Authorization") == "Bearer secret-token"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_http_401_logs_redacted_authorization_header(
        self, caplog: pytest.LogCaptureFixture
    ):
        """HTTP auth failures log redacted request headers without leaking provider token."""
        token = "crawl4ai-secret-token-12345"
        provider = Crawl4AIProvider(
            url="http://crawl4ai:11235",
            token=token,
            timeout_sec=5,
        )

        mock_client = AsyncMock()
        req = httpx.Request("POST", "http://crawl4ai:11235/crawl")
        resp = httpx.Response(401, request=req)
        _mock_stream(mock_client, resp)

        with caplog.at_level(
            logging.WARNING, logger="app.adapters.content.scraper.crawl4ai_provider"
        ):
            with patch.object(provider, "_get_client", return_value=mock_client):
                result = await provider.scrape_markdown("https://example.com")

        rendered = "\n".join(
            record.getMessage() + str(record.__dict__) for record in caplog.records
        )
        assert token not in rendered
        assert token not in (result.error_text or "")
        record = next(
            record for record in caplog.records if record.message == "crawl4ai_http_error"
        )
        request_headers = record.__dict__["request_headers"]
        assert request_headers["Authorization"] == "[REDACTED]"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_no_authorization_header_when_no_token(self):
        """No Authorization header when token is empty."""
        payload = _make_crawl_response(markdown="A" * 500)
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", token="", timeout_sec=5)

        mock_client = AsyncMock()
        _mock_stream(mock_client, _make_httpx_response(payload))

        with patch.object(provider, "_get_client", return_value=mock_client):
            await provider.scrape_markdown("https://example.com")

        call_kwargs = mock_client.stream.call_args
        headers = call_kwargs.kwargs.get("headers", {})
        assert "Authorization" not in headers

    @pytest.mark.asyncio(loop_scope="function")
    async def test_request_payload_pins_wire_format(self):
        """POST URL ends with /crawl and body JSON matches the expected wire format.

        Pins the v0.8.x endpoint contract so regressions are caught immediately.
        The provider uses POST /crawl with stream=False (single round-trip) and
        wraps nested configs in {type, params} envelopes as required by the v0.8.x API.
        """
        payload = _make_crawl_response(markdown="A" * 500)
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=5)

        mock_client = AsyncMock()
        _mock_stream(mock_client, _make_httpx_response(payload))

        with patch.object(provider, "_get_client", return_value=mock_client):
            await provider.scrape_markdown("https://example.com/article")

        call_args = mock_client.stream.call_args
        # The request is streamed as POST to the synchronous /crawl endpoint
        # (NOT /crawl/sync). args[0] is the HTTP method, args[1] is the URL.
        assert call_args.args[0] == "POST"
        assert call_args.args[1].endswith("/crawl"), (
            f"Expected POST URL ending in '/crawl', got: {call_args.args[1]!r}"
        )
        # Body JSON must match the exact wire format the provider sends
        expected_json = {
            "urls": ["https://example.com/article"],
            "browser_config": {
                "type": "BrowserConfig",
                "params": {"headless": True, "user_agent_mode": "random"},
            },
            "crawler_config": {
                "type": "CrawlerRunConfig",
                "params": {"cache_mode": "BYPASS", "stream": False},
            },
        }
        assert call_args.kwargs["json"] == expected_json

    @pytest.mark.asyncio(loop_scope="function")
    async def test_cache_mode_propagates_to_request_body(self):
        """cache_mode kwarg is forwarded into crawler_config.params in the request body."""
        payload = _make_crawl_response(markdown="A" * 500)
        provider = Crawl4AIProvider(
            url="http://crawl4ai:11235", timeout_sec=5, cache_mode="ENABLED"
        )

        mock_client = AsyncMock()
        _mock_stream(mock_client, _make_httpx_response(payload))

        with patch.object(provider, "_get_client", return_value=mock_client):
            await provider.scrape_markdown("https://example.com/article")

        call_args = mock_client.stream.call_args
        assert call_args.kwargs["json"]["crawler_config"]["params"]["cache_mode"] == "ENABLED"

    @pytest.mark.asyncio(loop_scope="function")
    async def test_httpx_timeout_exception_is_caught_as_timeout(self):
        """httpx.TimeoutException (not just stdlib TimeoutError) is caught and returns ERROR.

        Pins the (TimeoutError, httpx.TimeoutException) exception tuple in the provider
        so that network-level timeouts raised by httpx are handled gracefully.
        """
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=5)

        mock_client = AsyncMock()
        mock_client.stream = MagicMock(side_effect=httpx.TimeoutException("read timeout"))

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "crawl4ai"
        assert "timeout" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_oversize_declared_content_length_returns_error(self):
        """A Content-Length header declaring a size over the cap is rejected up front,
        before the body is read."""
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=5, max_response_mb=1)
        max_bytes = 1 * 1024 * 1024
        resp = _make_httpx_response(
            content=b"{}",
            extra_headers={"Content-Length": str(max_bytes + 1)},
        )

        mock_client = AsyncMock()
        _mock_stream(mock_client, resp)

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "crawl4ai"
        assert "byte limit" in (result.error_text or "").lower()

    @pytest.mark.asyncio(loop_scope="function")
    async def test_oversize_streamed_body_returns_error_despite_lying_content_length(self):
        """A response body whose actual streamed bytes exceed the cap is rejected even
        when Content-Length under-declares the true (larger) size."""
        provider = Crawl4AIProvider(url="http://crawl4ai:11235", timeout_sec=5, max_response_mb=1)
        max_bytes = 1 * 1024 * 1024
        oversized_body = b"x" * (max_bytes + 1000)
        resp = _make_httpx_response(
            content=oversized_body,
            extra_headers={"Content-Length": "2"},
        )

        mock_client = AsyncMock()
        _mock_stream(mock_client, resp)

        with patch.object(provider, "_get_client", return_value=mock_client):
            result = await provider.scrape_markdown("https://example.com")

        assert result.status == "error"
        assert result.endpoint == "crawl4ai"
        assert "byte limit" in (result.error_text or "").lower()
