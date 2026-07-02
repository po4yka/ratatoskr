from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.adapters.content.scraper.crawl4ai_provider import Crawl4AIProvider
from app.adapters.content.scraper.defuddle_provider import DefuddleProvider
from app.adapters.content.scraper.direct_html_provider import DirectHTMLProvider
from app.api.exceptions import AuthorizationError
from app.api.routers.proxy import proxy_image
from app.core.call_status import CallStatus

_SAFE_URL = (True, None)
_PRIVATE_REDIRECT = (False, "Hostname resolves to blocked address: 169.254.169.254")


class _AsyncContext:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *_args):
        return None


class _DirectHTMLClient:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.response = MagicMock(spec=httpx.Response)
        self.response.status_code = 302
        self.response.headers = {"location": "http://169.254.169.254/latest/meta-data/"}
        self.response.aclose = AsyncMock()

    def stream(self, _method: str, url: str, **_kwargs):
        self.urls.append(url)
        return _AsyncContext(self.response)


class _DefuddleClient:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.response = MagicMock(spec=httpx.Response)
        self.response.status_code = 302
        self.response.headers = {"location": "http://169.254.169.254/latest/meta-data/"}
        self.response.raise_for_status = MagicMock()

    async def get(self, url: str, **_kwargs):
        self.urls.append(url)
        return self.response


@pytest.mark.asyncio
async def test_proxy_image_blocks_redirect_to_private_ip() -> None:
    redirect = MagicMock(spec=httpx.Response)
    redirect.status_code = 302
    redirect.headers = {"location": "http://169.254.169.254/latest/meta-data/"}
    redirect.aclose = AsyncMock()
    client = MagicMock()
    client.build_request.return_value = MagicMock()
    client.send = AsyncMock(return_value=redirect)

    with (
        patch("httpx.AsyncClient", return_value=_AsyncContext(client)),
        patch("app.api.routers.proxy.is_url_safe", side_effect=[_SAFE_URL, _PRIVATE_REDIRECT]),
    ):
        with pytest.raises(AuthorizationError, match="blocked address"):
            await proxy_image("https://example.com/image.jpg")

    client.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_direct_html_provider_blocks_redirect_to_private_ip() -> None:
    client = _DirectHTMLClient()
    provider = DirectHTMLProvider(min_text_length=1)

    with (
        patch(
            "app.adapters.content.scraper.direct_html_provider.make_safe_async_client",
            return_value=_AsyncContext(client),
        ),
        patch(
            "app.adapters.content.scraper.direct_html_provider.is_url_safe",
            side_effect=[_SAFE_URL, _PRIVATE_REDIRECT],
        ),
    ):
        with pytest.raises(ValueError, match="SSRF blocked redirect target"):
            await provider._fetch_html("https://example.com/article")

    assert client.urls == ["https://example.com/article"]


@pytest.mark.asyncio
async def test_defuddle_provider_blocks_redirect_to_private_ip() -> None:
    client = _DefuddleClient()
    provider = DefuddleProvider(api_base_url="https://defuddle.example", min_content_length=1)

    with (
        patch(
            "app.adapters.content.scraper.defuddle_provider.make_safe_async_client",
            return_value=_AsyncContext(client),
        ),
        patch(
            "app.adapters.content.scraper.defuddle_provider.is_url_safe",
            # 1st call: target-URL input guard; 2nd: initial defuddle_url;
            # 3rd: the redirect target (private IP) which is blocked.
            side_effect=[_SAFE_URL, _SAFE_URL, _PRIVATE_REDIRECT],
        ),
    ):
        with pytest.raises(ValueError, match="SSRF blocked redirect target"):
            await provider._fetch_raw("https://example.com/article")

    assert client.urls == ["https://defuddle.example/https%3A%2F%2Fexample.com%2Farticle"]


@pytest.mark.asyncio
async def test_crawl4ai_provider_blocks_sidecar_redirect_to_private_ip() -> None:
    redirect = MagicMock(spec=httpx.Response)
    redirect.status_code = 302
    redirect.headers = {"location": "http://169.254.169.254/latest/meta-data/"}
    client = MagicMock()
    client.post = AsyncMock(return_value=redirect)
    provider = Crawl4AIProvider("http://crawl4ai:11235", min_content_length=1)
    provider._client = client

    with patch(
        "app.adapters.content.scraper.crawl4ai_provider.is_url_safe",
        return_value=_PRIVATE_REDIRECT,
    ):
        result = await provider.scrape_markdown("https://example.com/article")

    assert result.status == CallStatus.ERROR
    assert result.error_text is not None
    assert "SSRF blocked redirect target" in result.error_text
    client.post.assert_awaited_once()
