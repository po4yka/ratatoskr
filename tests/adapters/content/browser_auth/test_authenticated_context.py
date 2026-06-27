"""Tests for the PlaywrightAuthedFetcher enforcement layers."""

from __future__ import annotations

import pytest

from app.adapters.content.browser_auth import authenticated_context as ac
from app.adapters.content.browser_auth.authenticated_context import (
    HostNotAllowedError,
    PlaywrightAuthedFetcher,
    RequestCapExceededError,
    SSRFBlockedError,
)

_MOD = "app.adapters.content.browser_auth.authenticated_context"


class _FakeApiResp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _FakeRequest:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers, timeout))
        return _FakeApiResp(200, b'{"ok":true}')


class _FakeContext:
    def __init__(self) -> None:
        self.request = _FakeRequest()


def _allow_all_ssrf(monkeypatch) -> None:
    async def _safe(_url: str):
        return (True, None)

    monkeypatch.setattr(f"{_MOD}.is_url_safe_async", _safe)


async def test_host_not_allowed_raises_before_ssrf(monkeypatch) -> None:
    called = {"ssrf": False}

    async def _safe(_url: str):
        called["ssrf"] = True
        return (True, None)

    monkeypatch.setattr(f"{_MOD}.is_url_safe_async", _safe)
    f = PlaywrightAuthedFetcher(_FakeContext(), host_allowlist=["chatgpt.com"])
    with pytest.raises(HostNotAllowedError):
        await f.get("https://evil.com/api")
    assert called["ssrf"] is False  # allowlist short-circuits before the SSRF check


async def test_ssrf_block_raises(monkeypatch) -> None:
    async def _unsafe(_url: str):
        return (False, "private address")

    monkeypatch.setattr(f"{_MOD}.is_url_safe_async", _unsafe)
    f = PlaywrightAuthedFetcher(_FakeContext(), host_allowlist=["chatgpt.com"])
    with pytest.raises(SSRFBlockedError):
        await f.get("https://chatgpt.com/api")


async def test_request_cap(monkeypatch) -> None:
    _allow_all_ssrf(monkeypatch)
    monkeypatch.setattr(ac.asyncio, "sleep", _noop_sleep)
    f = PlaywrightAuthedFetcher(_FakeContext(), host_allowlist=["chatgpt.com"], max_requests=2)
    await f.get("https://chatgpt.com/a")
    await f.get("https://chatgpt.com/b")
    with pytest.raises(RequestCapExceededError):
        await f.get("https://chatgpt.com/c")
    assert f.requests_made == 2


async def test_delay_skipped_on_first_request(monkeypatch) -> None:
    _allow_all_ssrf(monkeypatch)
    sleeps: list[float] = []

    async def _record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(ac.asyncio, "sleep", _record_sleep)
    f = PlaywrightAuthedFetcher(
        _FakeContext(), host_allowlist=["chatgpt.com"], inter_request_delay_sec=1.0, jitter_sec=0.0
    )
    await f.get("https://chatgpt.com/a")
    assert sleeps == []  # no delay on the first request
    await f.get("https://chatgpt.com/b")
    assert sleeps == [1.0]


async def test_success_returns_body(monkeypatch) -> None:
    _allow_all_ssrf(monkeypatch)
    monkeypatch.setattr(ac.asyncio, "sleep", _noop_sleep)
    f = PlaywrightAuthedFetcher(_FakeContext(), host_allowlist=["chatgpt.com"])
    resp = await f.get("https://chatgpt.com/api", headers={"Authorization": "Bearer x"})
    assert resp.status == 200
    assert resp.json() == {"ok": True}


async def _noop_sleep(_seconds: float) -> None:
    return None
