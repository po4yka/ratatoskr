from __future__ import annotations

import socket
from unittest.mock import patch

import pytest

from app.core.urls.validation import (
    _resolve_hostname_to_addrs,
    async_validate_url_input,
    dns_cache_scope,
    validate_url_input,
)


def test_validate_url_input_rejects_localhost() -> None:
    with pytest.raises(ValueError, match="Localhost access not allowed"):
        validate_url_input("http://localhost/admin")


@pytest.mark.parametrize(
    ("url", "match"),
    [
        ("http://127.0.0.1/admin", r"Blocked IP address: 127\.0\.0\.1"),
        ("http://10.0.0.1/admin", r"Blocked IP address: 10\.0\.0\.1"),
        ("http://192.168.1.10/admin", r"Blocked IP address: 192\.168\.1\.10"),
        ("http://169.254.169.254/latest/meta-data/", r"Blocked IP address: 169\.254\.169\.254"),
        ("http://[fe80::1]/", r"Blocked IP address: fe80::1"),
        ("file:///etc/passwd", "URL scheme 'file' is not allowed"),
        ("git://example.com/repo", "URL scheme 'git' is not allowed"),
    ],
)
def test_validate_url_input_rejects_ssrf_and_non_http_urls(url: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_url_input(url)


def test_validate_url_input_rejects_suspicious_domain_suffix() -> None:
    with pytest.raises(ValueError, match=r"Suspicious domain pattern: \.internal"):
        validate_url_input("https://service.internal/path")


def test_validate_url_input_rejects_blocked_ip_literal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.security.ssrf.is_ip_blocked", lambda _ip: True)

    with pytest.raises(ValueError, match=r"Blocked IP address: 203\.0\.113\.10"):
        validate_url_input("https://203.0.113.10/path")


def test_validate_url_input_rejects_public_hostname_that_resolves_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.7", 0))],
    )

    with pytest.raises(ValueError, match=r"Hostname resolves to blocked IP address: 10\.0\.0\.7"):
        validate_url_input("https://public-looking.example/path")


def test_dns_cache_scope_reuses_hostname_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"count": 0}
    fake_response = [("family", "socktype", "proto", "", ("93.184.216.34", 0))]

    def fake_getaddrinfo(*_args, **_kwargs):
        calls["count"] += 1
        return fake_response

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with dns_cache_scope():
        first = _resolve_hostname_to_addrs("example.com", "example.com")
        second = _resolve_hostname_to_addrs("example.com", "example.com")

    uncached = _resolve_hostname_to_addrs("example.com", "example.com")

    assert first == second == uncached == fake_response
    assert calls["count"] == 2


# ---------------------------------------------------------------------------
# DNS-rebinding SSRF tests
#
# A DNS-rebinding attack works in two phases: (1) the validator resolves a
# public-looking hostname and sees a public IP, then (2) the HTTP client
# re-resolves and a malicious DNS server returns a private IP.  The tests
# below cover the validation-time half of that attack: when getaddrinfo
# returns a private address at check time the request is rejected.
#
# Residual TOCTOU limitation: because the HTTP client performs its own
# independent DNS resolution after validation, an adversary-controlled DNS
# server can still swap the IP between the two resolves.  Closing that gap
# requires connect-by-IP (pass the resolved address to the HTTP client
# directly) or a second post-connect IP check; neither is implemented here.
# ---------------------------------------------------------------------------


def test_validate_url_input_rejects_dns_rebinding_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync validator rejects a hostname whose DNS resolves to a private IP.

    This simulates the validation-time leg of a DNS-rebinding attack where
    getaddrinfo returns an RFC-1918 address for a public-looking hostname.
    """
    private_addr = "192.168.0.1"
    fake_result = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (private_addr, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: fake_result)

    with pytest.raises(
        ValueError, match=r"Hostname resolves to blocked IP address: 192\.168\.0\.1"
    ):
        validate_url_input("https://totally-public.example.com/path")


def test_validate_url_input_rejects_dns_rebinding_to_link_local(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync validator rejects a hostname resolving to a link-local (169.254.x.x) IP.

    Link-local addresses are commonly used for cloud metadata endpoints
    (e.g. AWS instance metadata at 169.254.169.254).
    """
    link_local_addr = "169.254.169.254"
    fake_result = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (link_local_addr, 0))]
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: fake_result)

    with pytest.raises(
        ValueError, match=r"Hostname resolves to blocked IP address: 169\.254\.169\.254"
    ):
        validate_url_input("https://cloud-metadata.example.com/path")


@pytest.mark.asyncio
async def test_async_validate_url_input_rejects_dns_rebinding_to_private_ip() -> None:
    """Async validator rejects a hostname whose DNS resolves to a private IP.

    Uses unittest.mock.patch to intercept socket.getaddrinfo inside the
    asyncio.to_thread worker, exercising the async code path independently.
    """
    private_addr = "10.0.0.1"
    fake_result = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (private_addr, 0))]

    with patch("socket.getaddrinfo", return_value=fake_result):
        with pytest.raises(
            ValueError, match=r"Hostname resolves to blocked IP address: 10\.0\.0\.1"
        ):
            await async_validate_url_input("https://totally-public.example.com/path")


@pytest.mark.asyncio
async def test_async_validate_url_input_rejects_dns_rebinding_to_loopback() -> None:
    """Async validator rejects a hostname resolving to the loopback range (127.x.x.x)."""
    loopback_addr = "127.0.0.1"
    fake_result = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (loopback_addr, 0))]

    with patch("socket.getaddrinfo", return_value=fake_result):
        with pytest.raises(
            ValueError, match=r"Hostname resolves to blocked IP address: 127\.0\.0\.1"
        ):
            await async_validate_url_input("https://not-loopback.example.com/path")


# ---------------------------------------------------------------------------
# Fail-closed on DNS failure
#
# When DNS resolution fails, the validator must REJECT (fail closed), matching
# the authoritative SSRF module (app/security/ssrf.py::is_url_safe). It used to
# swallow the resolver error into an empty address list, and the "reject if any
# resolved IP is blocked" loop then skipped it -- silently allowing an
# unresolvable host through (SSRF fail-open).
# ---------------------------------------------------------------------------


def test_validate_url_input_rejects_when_dns_resolution_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync validator rejects (not allows) a hostname whose DNS lookup errors."""

    def _boom(*_a, **_kw):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)

    with pytest.raises(ValueError, match=r"DNS resolution failed for unresolvable\.example\.com"):
        validate_url_input("https://unresolvable.example.com/path")


def test_validate_url_input_rejects_when_dns_returns_no_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sync validator rejects an empty resolution set (defensive fail-closed guard)."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a, **_kw: [])

    with pytest.raises(ValueError, match=r"No DNS records found for no-records\.example\.com"):
        validate_url_input("https://no-records.example.com/path")


@pytest.mark.asyncio
async def test_async_validate_url_input_rejects_when_dns_resolution_fails() -> None:
    """Async validator rejects a hostname whose DNS lookup errors."""
    with patch(
        "socket.getaddrinfo",
        side_effect=socket.gaierror(socket.EAI_NONAME, "Name or service not known"),
    ):
        with pytest.raises(
            ValueError, match=r"DNS resolution failed for unresolvable\.example\.com"
        ):
            await async_validate_url_input("https://unresolvable.example.com/path")
