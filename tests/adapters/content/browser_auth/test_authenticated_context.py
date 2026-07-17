"""Tests for the PlaywrightAuthedFetcher enforcement layers."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import pytest

from app.adapters.content.browser_auth import authenticated_context as ac
from app.adapters.content.browser_auth.authenticated_context import (
    HostNotAllowedError,
    PlaywrightAuthedFetcher,
    RequestCapExceededError,
    ResponseCapExceededError,
    SSRFBlockedError,
)

_MOD = "app.adapters.content.browser_auth.authenticated_context"


@dataclass
class _ResponsePlan:
    body: bytes = b'{"ok":true}'
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    redirect_url: str | None = None


class _FakeCdpSession:
    def __init__(self, plans: list[list[_ResponsePlan]]) -> None:
        self._plans = list(plans)
        self._listener = None
        self._hops: list[_ResponsePlan] = []
        self._hop = 0
        self._url = ""
        self._phase = ""
        self._stream_offset = 0
        self.sent: list[tuple[str, dict | None]] = []
        self.stream_bytes_read = 0

    def on(self, _event: str, listener) -> None:
        self._listener = listener

    def remove_listener(self, _event: str, listener) -> None:
        assert listener is self._listener
        self._listener = None

    def _emit_request(self, url: str) -> None:
        self._url = url
        self._phase = "request"
        assert self._listener is not None
        self._listener(
            {
                "requestId": f"request-{self._hop}",
                "request": {"url": url},
            }
        )

    def _emit_response(self) -> None:
        plan = self._hops[self._hop]
        headers = dict(plan.headers)
        if plan.redirect_url is not None:
            headers["location"] = plan.redirect_url
        self._phase = "response"
        assert self._listener is not None
        self._listener(
            {
                "requestId": f"request-{self._hop}",
                "request": {"url": self._url},
                "responseStatusCode": plan.status,
                "responseHeaders": [
                    {"name": name, "value": value} for name, value in headers.items()
                ],
            }
        )

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.sent.append((method, params))
        params = params or {}
        if method == "Page.navigate":
            self._hops = self._plans.pop(0)
            self._hop = 0
            self._emit_request(params["url"])
        elif method == "Fetch.continueRequest":
            if self._phase == "request":
                self._emit_response()
            else:
                plan = self._hops[self._hop]
                assert plan.redirect_url is not None
                self._hop += 1
                self._emit_request(plan.redirect_url)
        elif method == "Fetch.takeResponseBodyAsStream":
            self._stream_offset = 0
            return {"stream": "stream-1"}
        elif method == "IO.read":
            body = self._hops[self._hop].body
            size = params["size"]
            chunk = body[self._stream_offset : self._stream_offset + size]
            self._stream_offset += len(chunk)
            self.stream_bytes_read += len(chunk)
            return {
                "base64Encoded": True,
                "data": base64.b64encode(chunk).decode("ascii"),
                "eof": self._stream_offset >= len(body),
            }
        return {}


class _FakeContext:
    def __init__(self, plans: list[list[_ResponsePlan]] | None = None) -> None:
        self.session = _FakeCdpSession(plans or [[_ResponsePlan()]])

    async def new_cdp_session(self, _page) -> _FakeCdpSession:
        return self.session


class _FakePage:
    def __init__(self, plans: list[list[_ResponsePlan]] | None = None) -> None:
        self.context = _FakeContext(plans)
        self.headers: list[dict[str, str]] = []

    async def set_extra_http_headers(self, headers: dict[str, str]) -> None:
        self.headers.append(headers)


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
    f = PlaywrightAuthedFetcher(_FakePage(), host_allowlist=["chatgpt.com"])
    with pytest.raises(HostNotAllowedError):
        await f.get("https://evil.com/api")
    assert called["ssrf"] is False


async def test_ssrf_block_raises(monkeypatch) -> None:
    async def _unsafe(_url: str):
        return (False, "private address")

    monkeypatch.setattr(f"{_MOD}.is_url_safe_async", _unsafe)
    f = PlaywrightAuthedFetcher(_FakePage(), host_allowlist=["chatgpt.com"])
    with pytest.raises(SSRFBlockedError):
        await f.get("https://chatgpt.com/api")


async def test_request_cap(monkeypatch) -> None:
    _allow_all_ssrf(monkeypatch)
    monkeypatch.setattr(ac.asyncio, "sleep", _noop_sleep)
    page = _FakePage([[_ResponsePlan()], [_ResponsePlan()]])
    f = PlaywrightAuthedFetcher(page, host_allowlist=["chatgpt.com"], max_requests=2)
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
    page = _FakePage([[_ResponsePlan()], [_ResponsePlan()]])
    f = PlaywrightAuthedFetcher(
        page, host_allowlist=["chatgpt.com"], inter_request_delay_sec=1.0, jitter_sec=0.0
    )
    await f.get("https://chatgpt.com/a")
    assert sleeps == []
    await f.get("https://chatgpt.com/b")
    assert sleeps == [1.0]


async def test_success_returns_body_and_preserves_headers(monkeypatch) -> None:
    _allow_all_ssrf(monkeypatch)
    page = _FakePage()
    f = PlaywrightAuthedFetcher(page, host_allowlist=["chatgpt.com"])
    resp = await f.get("https://chatgpt.com/api", headers={"Authorization": "Bearer x"})
    assert resp.status == 200
    assert resp.json() == {"ok": True}
    assert page.headers == [{"Authorization": "Bearer x"}]


async def test_declared_response_size_is_rejected_before_stream_read(monkeypatch) -> None:
    _allow_all_ssrf(monkeypatch)
    page = _FakePage([[_ResponsePlan(body=b"must not be read", headers={"content-length": "100"})]])
    f = PlaywrightAuthedFetcher(page, host_allowlist=["chatgpt.com"], max_response_bytes=10)
    with pytest.raises(ResponseCapExceededError, match="Content-Length"):
        await f.get("https://chatgpt.com/api")
    assert page.context.session.stream_bytes_read == 0


async def test_chunked_response_is_stopped_while_streaming(monkeypatch) -> None:
    _allow_all_ssrf(monkeypatch)
    body = b"x" * 1_000_000
    page = _FakePage([[_ResponsePlan(body=body, headers={"transfer-encoding": "chunked"})]])
    f = PlaywrightAuthedFetcher(
        page,
        host_allowlist=["chatgpt.com"],
        max_response_bytes=10,
        max_run_bytes=100,
    )
    with pytest.raises(ResponseCapExceededError, match="response body"):
        await f.get("https://chatgpt.com/api")
    assert page.context.session.stream_bytes_read == 11


async def test_underreported_content_length_does_not_bypass_stream_cap(monkeypatch) -> None:
    _allow_all_ssrf(monkeypatch)
    page = _FakePage([[_ResponsePlan(body=b"x" * 1_000_000, headers={"content-length": "1"})]])
    f = PlaywrightAuthedFetcher(
        page,
        host_allowlist=["chatgpt.com"],
        max_response_bytes=10,
        max_run_bytes=100,
    )
    with pytest.raises(ResponseCapExceededError, match="response body"):
        await f.get("https://chatgpt.com/api")
    assert page.context.session.stream_bytes_read == 11


async def test_response_without_content_length_obeys_aggregate_stream_cap(monkeypatch) -> None:
    _allow_all_ssrf(monkeypatch)
    monkeypatch.setattr(ac.asyncio, "sleep", _noop_sleep)
    page = _FakePage(
        [
            [_ResponsePlan(body=b"123456")],
            [_ResponsePlan(body=b"abcdef")],
        ]
    )
    f = PlaywrightAuthedFetcher(
        page,
        host_allowlist=["chatgpt.com"],
        max_response_bytes=20,
        max_run_bytes=10,
    )
    await f.get("https://chatgpt.com/a")
    with pytest.raises(ResponseCapExceededError, match="run response bytes"):
        await f.get("https://chatgpt.com/b")
    assert f.bytes_received == 6
    assert page.context.session.stream_bytes_read == 11


async def test_redirect_to_disallowed_host_is_refused(monkeypatch) -> None:
    _allow_all_ssrf(monkeypatch)
    monkeypatch.setattr(ac.asyncio, "sleep", _noop_sleep)
    page = _FakePage(
        [
            [
                _ResponsePlan(
                    status=302,
                    redirect_url="https://169.254.169.254/latest/meta-data/",
                ),
                _ResponsePlan(),
            ]
        ]
    )
    f = PlaywrightAuthedFetcher(page, host_allowlist=["chatgpt.com"])
    with pytest.raises(HostNotAllowedError):
        await f.get("https://chatgpt.com/api")


async def test_redirect_to_allowed_host_is_followed(monkeypatch) -> None:
    _allow_all_ssrf(monkeypatch)
    monkeypatch.setattr(ac.asyncio, "sleep", _noop_sleep)
    page = _FakePage(
        [
            [
                _ResponsePlan(status=302, redirect_url="https://chatgpt.com/final"),
                _ResponsePlan(),
            ]
        ]
    )
    f = PlaywrightAuthedFetcher(page, host_allowlist=["chatgpt.com"])
    resp = await f.get("https://chatgpt.com/api")
    assert resp.status == 200
    assert resp.json() == {"ok": True}
    assert f.requests_made == 2


async def test_redirect_revalidates_ssrf_on_each_hop(monkeypatch) -> None:
    async def _safe(url: str):
        return ("evil.internal" not in url, "private" if "evil.internal" in url else None)

    monkeypatch.setattr(f"{_MOD}.is_url_safe_async", _safe)
    monkeypatch.setattr(ac.asyncio, "sleep", _noop_sleep)
    page = _FakePage(
        [
            [
                _ResponsePlan(status=302, redirect_url="https://evil.internal/x"),
                _ResponsePlan(),
            ]
        ]
    )
    f = PlaywrightAuthedFetcher(page, host_allowlist=["chatgpt.com", "evil.internal"])
    with pytest.raises(SSRFBlockedError):
        await f.get("https://chatgpt.com/api")


async def _noop_sleep(_seconds: float) -> None:
    return None
