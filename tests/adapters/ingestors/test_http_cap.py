"""Unit tests for the ingestors' size-capped JSON fetch helper.

Covers both cap paths (declared Content-Length and streamed bytes) and the
check_status pre-body hook, so the tiny caps here need no real megabyte bodies.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.adapters.ingestors._http import fetch_json_capped
from app.application.ports.source_ingestors import TransientSourceError

pytestmark = pytest.mark.no_network


class _FakeResponse:
    def __init__(
        self,
        *,
        body: bytes,
        headers: dict[str, str] | None = None,
        status_code: int = 200,
        chunk_size: int = 8,
    ) -> None:
        self._body = body
        self.headers = headers or {}
        self.status_code = status_code
        self._chunk_size = chunk_size
        self.bytes_iterated = 0

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        for i in range(0, len(self._body), self._chunk_size):
            chunk = self._body[i : i + self._chunk_size]
            self.bytes_iterated += len(chunk)
            yield chunk


class _FakeClient:
    def __init__(self, *, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self._headers = headers or {}
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self.last_response: _FakeResponse | None = None

    def stream(
        self, method: str, url: str, *, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self.calls.append((url, headers))
        self.last_response = _FakeResponse(body=self._body, headers=self._headers)
        return self.last_response


@pytest.mark.asyncio
async def test_parses_small_body() -> None:
    client = _FakeClient(body=json.dumps({"ok": True}).encode())
    payload = await fetch_json_capped(client, "https://api/x", max_bytes=1024, provider="Test")
    assert payload == {"ok": True}
    assert client.calls == [("https://api/x", None)]


@pytest.mark.asyncio
async def test_forwards_headers() -> None:
    client = _FakeClient(body=b"{}")
    await fetch_json_capped(
        client, "https://api/x", max_bytes=1024, provider="Test", headers={"User-Agent": "UA"}
    )
    assert client.calls == [("https://api/x", {"User-Agent": "UA"})]


@pytest.mark.asyncio
async def test_rejects_streamed_body_over_cap() -> None:
    # No Content-Length header, so the cumulative byte count is the only guard.
    client = _FakeClient(body=b"a" * 4096)
    with pytest.raises(TransientSourceError, match="byte cap"):
        await fetch_json_capped(client, "https://api/x", max_bytes=64, provider="Test")


@pytest.mark.asyncio
async def test_rejects_declared_content_length_over_cap() -> None:
    client = _FakeClient(body=b"{}", headers={"content-length": str(50 * 1024 * 1024)})
    with pytest.raises(TransientSourceError, match="byte cap"):
        await fetch_json_capped(client, "https://api/x", max_bytes=1024, provider="Test")
    # The oversized declaration short-circuits before the body is streamed.
    assert client.last_response is not None
    assert client.last_response.bytes_iterated == 0


@pytest.mark.asyncio
async def test_check_status_runs_before_body_and_can_raise() -> None:
    client = _FakeClient(body=b"{}")

    def _reject(_response: Any) -> None:
        raise TransientSourceError("status boom")

    with pytest.raises(TransientSourceError, match="status boom"):
        await fetch_json_capped(
            client, "https://api/x", max_bytes=1024, provider="Test", check_status=_reject
        )
    assert client.last_response is not None
    assert client.last_response.bytes_iterated == 0
