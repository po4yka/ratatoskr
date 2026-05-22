from __future__ import annotations

import socket

import pytest

from app.core.urls.validation import _resolve_hostname_to_addrs, dns_cache_scope, validate_url_input


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
