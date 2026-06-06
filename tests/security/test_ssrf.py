"""Tests for SSRF protection: blocked-IP payloads, transport IP-pinning, DNS-rebinding."""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.security.ssrf import allow_private_network_urls, is_ip_blocked, is_url_safe

# ---------------------------------------------------------------------------
# is_ip_blocked — individual IP payload checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.1",
        "10.255.255.255",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.0.1",
        "192.168.255.255",
        "169.254.169.254",  # AWS metadata
        "100.64.0.1",  # carrier-grade NAT
        "224.0.0.1",  # multicast
        "255.255.255.255",  # broadcast
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 private
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:192.168.1.1",  # IPv4-mapped private
        "64:ff9b::1",  # NAT64 prefix
        "2002:c0a8:0101::",  # 6to4 wrapping 192.168.1.1
    ],
)
def test_is_ip_blocked_returns_true_for_private_addresses(ip: str) -> None:
    assert is_ip_blocked(ip) is True


def test_is_ip_blocked_returns_false_for_public_ipv4() -> None:
    assert is_ip_blocked("93.184.216.34") is False


def test_is_ip_blocked_returns_false_for_public_ipv6() -> None:
    assert is_ip_blocked("2001:db8:85a3::8a2e:370:7334") is False


def test_is_ip_blocked_returns_true_for_unparseable_input() -> None:
    # Unparseable → treated as blocked for safety
    assert is_ip_blocked("not-an-ip") is True


# ---------------------------------------------------------------------------
# is_url_safe — URL-level checks
# ---------------------------------------------------------------------------


def test_is_url_safe_blocks_localhost_name() -> None:
    safe, reason = is_url_safe("http://localhost/")
    assert safe is False
    assert reason is not None


def test_is_url_safe_blocks_non_http_scheme() -> None:
    safe, reason = is_url_safe("file:///etc/passwd")
    assert safe is False
    assert reason is not None
    assert "scheme" in reason.lower()


def test_is_url_safe_allows_public_hostname_after_dns_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.security.ssrf.resolve_host_ips", lambda _: ["93.184.216.34"])
    safe, reason = is_url_safe("https://example.com/article")
    assert safe is True
    assert reason is None


def test_is_url_safe_blocks_ipv4_loopback_literal() -> None:
    safe, _ = is_url_safe("http://127.0.0.1/")
    assert safe is False


def test_is_url_safe_blocks_rfc1918_literal() -> None:
    safe, _ = is_url_safe("http://192.168.1.1/")
    assert safe is False


def test_is_url_safe_blocks_ipv6_loopback_literal() -> None:
    safe, _ = is_url_safe("http://[::1]/")
    assert safe is False


def test_is_url_safe_blocks_aws_metadata_ip() -> None:
    safe, _ = is_url_safe("http://169.254.169.254/latest/meta-data/")
    assert safe is False


def test_local_dev_override_allows_localhost_and_rfc1918_but_not_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCRAPER_ALLOW_PRIVATE_NETWORK_URLS", "true")
    assert allow_private_network_urls() is True
    assert is_url_safe("http://localhost:8000/")[0] is True
    assert is_url_safe("http://10.0.0.5/")[0] is True
    assert is_url_safe("http://169.254.169.254/latest/meta-data/")[0] is False


def test_is_url_safe_blocks_ipv4_mapped_ipv6() -> None:
    safe, _ = is_url_safe("http://[::ffff:7f00:1]/")  # ::ffff:127.0.0.1
    assert safe is False


def test_is_url_safe_returns_false_on_dns_failure() -> None:
    with patch("app.security.ssrf.resolve_host_ips", side_effect=socket.gaierror("NXDOMAIN")):
        safe, _ = is_url_safe("http://does-not-exist.invalid/")
    assert safe is False


def test_is_url_safe_blocks_hostname_that_resolves_to_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Patch resolve_host_ips to return a private IP; is_url_safe must block the request.
    # The reason string may reference the resolved IP or the hostname depending on
    # whether is_ip_blocked short-circuits before DNS resolution.
    monkeypatch.setattr("app.security.ssrf.resolve_host_ips", lambda _: ["192.168.1.1"])
    safe, reason = is_url_safe("http://evil.example.com/")
    assert safe is False
    assert reason is not None


# ---------------------------------------------------------------------------
# SafeAsyncTransport — IP-pinning and DNS-rebinding prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safe_async_transport_blocks_private_ip_literal() -> None:
    """Transport raises ConnectError for an IP-literal URL in a blocked range."""
    from app.security.ssrf import SafeAsyncTransport

    transport = SafeAsyncTransport()
    request = httpx.Request("GET", "http://192.168.1.1/")
    with pytest.raises(httpx.ConnectError, match="SSRF blocked"):
        await transport.handle_async_request(request)


@pytest.mark.asyncio
async def test_safe_async_transport_blocks_ipv6_loopback() -> None:
    from app.security.ssrf import SafeAsyncTransport

    transport = SafeAsyncTransport()
    request = httpx.Request("GET", "http://[::1]/")
    with pytest.raises(httpx.ConnectError, match="SSRF blocked"):
        await transport.handle_async_request(request)


@pytest.mark.asyncio
async def test_safe_async_transport_blocks_aws_metadata() -> None:
    from app.security.ssrf import SafeAsyncTransport

    transport = SafeAsyncTransport()
    request = httpx.Request("GET", "http://169.254.169.254/latest/meta-data/")
    with pytest.raises(httpx.ConnectError, match="SSRF blocked"):
        await transport.handle_async_request(request)


@pytest.mark.asyncio
async def test_safe_async_transport_blocks_non_http_scheme() -> None:
    from app.security.ssrf import SafeAsyncTransport

    transport = SafeAsyncTransport()
    request = httpx.Request("GET", "ftp://example.com/")
    with pytest.raises(httpx.ConnectError, match="Blocked scheme"):
        await transport.handle_async_request(request)


@pytest.mark.asyncio
async def test_safe_async_transport_blocks_if_any_resolved_ip_is_private() -> None:
    """All resolved IPs are checked — one private IP poisons the whole response."""
    from app.security.ssrf import SafeAsyncTransport

    transport = SafeAsyncTransport()
    request = httpx.Request("GET", "http://example.com/")

    def fake_getaddrinfo(host: str, port: Any, **_: Any) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.1", port)),
        ]

    with patch("app.security.ssrf.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.raises(httpx.ConnectError, match="SSRF blocked"):
            await transport.handle_async_request(request)


@pytest.mark.asyncio
async def test_safe_async_transport_dns_rebinding_blocked() -> None:
    """Simulates DNS rebinding: transport resolves private IP at connect time and blocks it.

    Before this transport existed, a preflight check would pass (public IP on first
    lookup) and httpcore would re-resolve to a private IP at connect time.  The
    transport closes that window by being the resolver.
    """
    from app.security.ssrf import SafeAsyncTransport

    def rebinding_getaddrinfo(host: str, port: Any, **_: Any) -> list[Any]:
        # Simulate rebinding: always returns the private IP when the transport calls it
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("10.0.0.1", port))]

    transport = SafeAsyncTransport()
    request = httpx.Request("GET", "http://rebind.example.com/")

    with patch("app.security.ssrf.socket.getaddrinfo", side_effect=rebinding_getaddrinfo):
        with pytest.raises(httpx.ConnectError, match="SSRF blocked"):
            await transport.handle_async_request(request)


@pytest.mark.asyncio
async def test_safe_async_transport_blocks_redirect_to_private() -> None:
    """Second call to the transport (redirect hop) is blocked when target is private."""
    from app.security.ssrf import SafeAsyncTransport

    def fake_getaddrinfo(host: str, port: Any, **_: Any) -> list[Any]:
        if host == "example.com":
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port))]
        # redirect target resolves to private
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.168.1.1", port))]

    transport = SafeAsyncTransport()
    # Simulate the redirect hop: caller already followed the redirect and
    # now calls the transport with the Location URL directly.
    redirect_request = httpx.Request("GET", "http://internal.corp/secret")

    with patch("app.security.ssrf.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.raises(httpx.ConnectError, match="SSRF blocked"):
            await transport.handle_async_request(redirect_request)


@pytest.mark.asyncio
async def test_safe_async_transport_pins_ip_in_forwarded_request() -> None:
    """Transport rewrites URL host to the resolved IP before calling super(), preventing re-resolution."""
    from app.security.ssrf import SafeAsyncTransport

    captured: dict[str, Any] = {}

    async def mock_super_handler(self: Any, request: httpx.Request) -> httpx.Response:
        captured["url_host"] = request.url.host
        captured["host_header"] = request.headers.get("host")
        return httpx.Response(200, content=b"ok")

    def fake_getaddrinfo(host: str, port: Any, **_: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port))]

    transport = SafeAsyncTransport()
    request = httpx.Request("GET", "http://example.com/")

    with patch("app.security.ssrf.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        with patch.object(
            httpx.AsyncHTTPTransport,
            "handle_async_request",
            new=mock_super_handler,
        ):
            await transport.handle_async_request(request)

    assert captured["url_host"] == "93.184.216.34", "URL must use pinned IP, not hostname"
    assert captured["host_header"] == "example.com", "Host header must be original hostname"


@pytest.mark.asyncio
async def test_safe_async_transport_sets_sni_for_https() -> None:
    """For HTTPS requests, transport adds sni_hostname so cert validation still works."""
    from app.security.ssrf import SafeAsyncTransport

    captured: dict[str, Any] = {}

    async def mock_super_handler(self: Any, request: httpx.Request) -> httpx.Response:
        captured["extensions"] = dict(request.extensions)
        return httpx.Response(200, content=b"ok")

    def fake_getaddrinfo(host: str, port: Any, **_: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", port))]

    transport = SafeAsyncTransport()
    request = httpx.Request("GET", "https://example.com/")

    with patch("app.security.ssrf.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        with patch.object(
            httpx.AsyncHTTPTransport,
            "handle_async_request",
            new=mock_super_handler,
        ):
            await transport.handle_async_request(request)

    assert captured["extensions"].get("sni_hostname") == b"example.com"


@pytest.mark.asyncio
async def test_safe_async_transport_raises_on_dns_failure() -> None:
    from app.security.ssrf import SafeAsyncTransport

    transport = SafeAsyncTransport()
    request = httpx.Request("GET", "http://nxdomain.example.invalid/")

    with patch(
        "app.security.ssrf.socket.getaddrinfo",
        side_effect=socket.gaierror("NXDOMAIN"),
    ):
        with pytest.raises(httpx.ConnectError, match="DNS resolution failed"):
            await transport.handle_async_request(request)


# SafeSyncTransport


def test_safe_sync_transport_blocks_private_ip_literal() -> None:
    from app.security.ssrf import SafeSyncTransport

    transport = SafeSyncTransport()
    request = httpx.Request("GET", "http://10.0.0.1/")
    with pytest.raises(httpx.ConnectError, match="SSRF blocked"):
        transport.handle_request(request)


def test_safe_sync_transport_blocks_dns_rebinding() -> None:
    from app.security.ssrf import SafeSyncTransport

    def fake_getaddrinfo(host: str, port: Any, **_: Any) -> list[Any]:
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("172.16.0.1", port))]

    transport = SafeSyncTransport()
    request = httpx.Request("GET", "http://rebind.example.com/")

    with patch("app.security.ssrf.socket.getaddrinfo", side_effect=fake_getaddrinfo):
        with pytest.raises(httpx.ConnectError, match="SSRF blocked"):
            transport.handle_request(request)


def test_safe_sync_transport_blocks_non_http_scheme() -> None:
    from app.security.ssrf import SafeSyncTransport

    transport = SafeSyncTransport()
    request = httpx.Request("GET", "ftp://example.com/")
    with pytest.raises(httpx.ConnectError, match="Blocked scheme"):
        transport.handle_request(request)


def test_safe_sync_transport_raises_on_dns_failure() -> None:
    from app.security.ssrf import SafeSyncTransport

    transport = SafeSyncTransport()
    request = httpx.Request("GET", "http://nxdomain.example.invalid/")

    with patch(
        "app.security.ssrf.socket.getaddrinfo",
        side_effect=socket.gaierror("NXDOMAIN"),
    ):
        with pytest.raises(httpx.ConnectError, match="DNS resolution failed"):
            transport.handle_request(request)


# ---------------------------------------------------------------------------
# resolve_host_ips_async / is_url_safe_async — non-blocking DNS with retry
# ---------------------------------------------------------------------------

_ADDRINFO_PUBLIC: list[Any] = [
    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", 0))
]


@pytest.mark.asyncio
async def test_resolve_host_ips_async_retries_transient_failure() -> None:
    from app.security.ssrf import resolve_host_ips_async

    with patch(
        "socket.getaddrinfo",
        side_effect=[socket.gaierror("transient resolver blip"), _ADDRINFO_PUBLIC],
    ) as mock_gai:
        ips = await resolve_host_ips_async("example.com", retries=1, retry_delay_sec=0)

    assert ips == ["93.184.216.34"]
    assert mock_gai.call_count == 2


@pytest.mark.asyncio
async def test_resolve_host_ips_async_raises_after_retries_exhausted() -> None:
    from app.security.ssrf import resolve_host_ips_async

    with patch("socket.getaddrinfo", side_effect=socket.gaierror("resolver down")) as mock_gai:
        with pytest.raises(socket.gaierror):
            await resolve_host_ips_async("example.com", retries=1, retry_delay_sec=0)

    assert mock_gai.call_count == 2


@pytest.mark.asyncio
async def test_is_url_safe_async_reports_dns_failure_as_retryable() -> None:
    from app.security.ssrf import is_dns_failure_reason, is_url_safe_async

    with patch("socket.getaddrinfo", side_effect=socket.gaierror("resolver down")):
        safe, reason = await is_url_safe_async("https://example.com/article", dns_retries=0)

    assert safe is False
    assert reason is not None
    assert is_dns_failure_reason(reason) is True


@pytest.mark.asyncio
async def test_is_url_safe_async_allows_public_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.security.ssrf import is_url_safe_async

    async def fake_resolve(hostname: str, **kwargs: Any) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr("app.security.ssrf.resolve_host_ips_async", fake_resolve)
    safe, reason = await is_url_safe_async("https://example.com/article")

    assert safe is True
    assert reason is None


@pytest.mark.asyncio
async def test_is_url_safe_async_blocks_private_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.security.ssrf import is_dns_failure_reason, is_url_safe_async

    async def fake_resolve(hostname: str, **kwargs: Any) -> list[str]:
        return ["127.0.0.1"]

    monkeypatch.setattr("app.security.ssrf.resolve_host_ips_async", fake_resolve)
    safe, reason = await is_url_safe_async("https://internal.example.com/")

    assert safe is False
    assert reason is not None
    assert is_dns_failure_reason(reason) is False


@pytest.mark.asyncio
async def test_is_url_safe_async_blocks_ip_literal_without_dns() -> None:
    from app.security.ssrf import is_url_safe_async

    safe, _ = await is_url_safe_async("http://169.254.169.254/latest/meta-data/")
    assert safe is False


def test_is_dns_failure_reason_distinguishes_policy_blocks() -> None:
    from app.security.ssrf import is_dns_failure_reason

    assert is_dns_failure_reason("DNS resolution failed for example.com") is True
    assert is_dns_failure_reason("Localhost is not allowed") is False
    assert is_dns_failure_reason("Hostname resolves to blocked address: 127.0.0.1") is False
    assert is_dns_failure_reason(None) is False
