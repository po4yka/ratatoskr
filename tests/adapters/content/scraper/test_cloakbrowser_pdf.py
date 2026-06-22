"""Unit tests for CloakBrowserProvider PDF helpers (no real browser needed)."""

from __future__ import annotations

from typing import Any

import pytest

from app.adapters.content.scraper.cloakbrowser_provider import CloakBrowserProvider

pytestmark = pytest.mark.no_network


def _provider() -> CloakBrowserProvider:
    return CloakBrowserProvider(endpoint_url="http://cloak:9222")


def test_validate_pdf_accepts_pdf_bytes() -> None:
    body = b"%PDF-1.7 stream..."
    assert CloakBrowserProvider._validate_pdf(body, "src") == body


def test_validate_pdf_accepts_leading_whitespace() -> None:
    body = b"   \n%PDF-1.5 ..."
    assert CloakBrowserProvider._validate_pdf(body, "src") == body


def test_validate_pdf_rejects_html_challenge() -> None:
    assert CloakBrowserProvider._validate_pdf(b"<html>cf challenge</html>", "src") is None


def test_validate_pdf_rejects_none() -> None:
    assert CloakBrowserProvider._validate_pdf(None, "src") is None


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakePath:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read_bytes(self) -> bytes:
        return self._data


class _FakeDownload:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def path(self) -> _FakePath:
        return _FakePath(self._data)


@pytest.mark.asyncio
async def test_read_pdf_body_from_response() -> None:
    body = await CloakBrowserProvider._read_pdf_body(
        [], _FakeResponse(b"%PDF data"), max_bytes=1_000
    )
    assert body == b"%PDF data"


@pytest.mark.asyncio
async def test_read_pdf_body_rejects_oversize_response() -> None:
    body = await CloakBrowserProvider._read_pdf_body([], _FakeResponse(b"x" * 100), max_bytes=10)
    assert body is None


@pytest.mark.asyncio
async def test_read_pdf_body_prefers_download() -> None:
    body = await CloakBrowserProvider._read_pdf_body(
        [_FakeDownload(b"%PDF download")], _FakeResponse(b"ignored"), max_bytes=1_000
    )
    assert body == b"%PDF download"


@pytest.mark.asyncio
async def test_read_pdf_body_none_when_no_source() -> None:
    assert await CloakBrowserProvider._read_pdf_body([], None, max_bytes=1_000) is None


@pytest.mark.asyncio
async def test_fetch_pdf_ssrf_blocked_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _blocked(_url: str) -> tuple[bool, str]:
        return (False, "private network")

    monkeypatch.setattr(
        "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async", _blocked
    )
    out = await _provider().fetch_pdf(
        "https://x/landing", "http://10.0.0.1/secret.pdf", max_bytes=1_000
    )
    assert out is None


@pytest.mark.asyncio
async def test_download_via_controls_ssrf_blocked_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _blocked(_url: str) -> tuple[bool, str]:
        return (False, "private network")

    async def _picker(_c: list[dict[str, Any]]) -> dict[str, Any] | None:
        raise AssertionError("picker must not run when the landing URL is blocked")

    monkeypatch.setattr(
        "app.adapters.content.scraper.cloakbrowser_provider.is_url_safe_async", _blocked
    )
    out = await _provider().download_pdf_via_controls(
        "http://169.254.169.254/", picker=_picker, max_bytes=1_000
    )
    assert out is None
