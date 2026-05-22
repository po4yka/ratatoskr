from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import RequestError, Response

from app.api.exceptions import ExternalAPIError, ResourceNotFoundError, ValidationError
from app.api.routers.proxy import proxy_image

_SAFE_URL = (True, None)


@pytest.fixture(autouse=True)
def _bypass_ssrf_check():
    """Bypass SSRF DNS resolution in proxy tests -- tested separately in test_ssrf.py."""
    with patch("app.api.routers.proxy.is_url_safe", return_value=_SAFE_URL):
        yield


async def _aiter_bytes(chunks: list[bytes]):
    for chunk in chunks:
        yield chunk


def _mock_async_client(mock_client_cls, mock_response: MagicMock | None = None) -> MagicMock:
    mock_client = MagicMock()
    mock_client.build_request.return_value = MagicMock()
    mock_client.send = AsyncMock(return_value=mock_response)
    mock_context = MagicMock()
    mock_context.__aenter__ = AsyncMock(return_value=mock_client)
    mock_context.__aexit__ = AsyncMock(return_value=None)
    mock_client_cls.return_value = mock_context
    return mock_client


@pytest.mark.asyncio
async def test_proxy_image_success():
    """Test successful image proxying."""
    mock_response = MagicMock(spec=Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/jpeg"}
    mock_response.aiter_bytes = lambda: _aiter_bytes([b"fake", b"image"])
    mock_response.aclose = AsyncMock()

    # Mock the context manager behavior of AsyncClient
    with patch("httpx.AsyncClient") as mock_client_cls:
        _mock_async_client(mock_client_cls, mock_response)

        response = await proxy_image("https://example.com/image.jpg")

        assert response.status_code == 200
        assert response.media_type == "image/jpeg"
        assert response.body == b"fakeimage"


@pytest.mark.asyncio
async def test_proxy_image_invalid_scheme():
    """Test rejection of non-http/https URLs."""
    with pytest.raises(ValidationError):
        await proxy_image("ftp://example.com/image.jpg")


@pytest.mark.asyncio
async def test_proxy_image_not_found():
    """Test handling of 404 from upstream."""
    mock_response = MagicMock(spec=Response)
    mock_response.status_code = 404
    mock_response.aclose = AsyncMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        _mock_async_client(mock_client_cls, mock_response)

        with pytest.raises(ResourceNotFoundError):
            await proxy_image("https://example.com/missing.jpg")


@pytest.mark.asyncio
async def test_proxy_image_not_an_image():
    """Test rejection of non-image content types."""
    mock_response = MagicMock(spec=Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "text/html"}
    mock_response.aclose = AsyncMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        _mock_async_client(mock_client_cls, mock_response)

        with pytest.raises(ValidationError):
            await proxy_image("https://example.com/page.html")


@pytest.mark.asyncio
async def test_proxy_image_request_error():
    """Test handling of network errors."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = _mock_async_client(mock_client_cls)
        mock_client.send.side_effect = RequestError("Connection failed")

        with pytest.raises(ExternalAPIError):
            await proxy_image("https://example.com/image.jpg")


@pytest.mark.asyncio
async def test_proxy_image_rejects_declared_too_large_content():
    """Declared content-length above limit should be rejected with 413."""
    mock_response = MagicMock(spec=Response)
    mock_response.status_code = 200
    mock_response.headers = {
        "content-type": "image/jpeg",
        "content-length": str(11 * 1024 * 1024),
    }
    mock_response.aclose = AsyncMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        _mock_async_client(mock_client_cls, mock_response)

        with pytest.raises(ValidationError):
            await proxy_image("https://example.com/huge.jpg")


@pytest.mark.asyncio
async def test_proxy_image_rejects_stream_too_large_content():
    """Streaming content above limit should be rejected with 413."""
    ten_mb = 10 * 1024 * 1024
    mock_response = MagicMock(spec=Response)
    mock_response.status_code = 200
    mock_response.headers = {"content-type": "image/jpeg"}
    mock_response.aiter_bytes = lambda: _aiter_bytes([b"x" * ten_mb, b"y"])
    mock_response.aclose = AsyncMock()

    with patch("httpx.AsyncClient") as mock_client_cls:
        _mock_async_client(mock_client_cls, mock_response)

        with pytest.raises(ValidationError):
            await proxy_image("https://example.com/huge-stream.jpg")
