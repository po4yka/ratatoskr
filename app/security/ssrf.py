"""Centralized SSRF protection.

Single source of truth for blocked-network definitions, hostname resolution,
and URL safety checks used across the codebase (proxy, RSS fetcher, webhooks,
URL validation).
"""

from __future__ import annotations

import asyncio
import os
import socket
from ipaddress import ip_address, ip_network
from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.logging_utils import get_logger

logger = get_logger(__name__)

_TRUE_VALUES = {"1", "true", "yes", "on"}
_ALLOWED_SCHEMES = {"http", "https"}

# Local/private ranges that may be allowed only by an explicit local-development override.
LOCAL_DEV_OVERRIDABLE_NETWORKS = [
    ip_network("10.0.0.0/8"),  # Private Class A
    ip_network("172.16.0.0/12"),  # Private Class B
    ip_network("192.168.0.0/16"),  # Private Class C
    ip_network("127.0.0.0/8"),  # Loopback
    ip_network("::1/128"),  # IPv6 loopback
    ip_network("fc00::/7"),  # IPv6 private
]

# Private/internal IP ranges that must be blocked to prevent SSRF.
ALWAYS_BLOCKED_NETWORKS = [
    ip_network("169.254.0.0/16"),  # Link-local / AWS metadata
    ip_network("0.0.0.0/8"),  # Current network
    ip_network("100.64.0.0/10"),  # Carrier-grade NAT
    ip_network("192.0.0.0/24"),  # IETF Protocol Assignments
    ip_network("192.0.2.0/24"),  # TEST-NET-1
    ip_network("198.51.100.0/24"),  # TEST-NET-2
    ip_network("203.0.113.0/24"),  # TEST-NET-3
    ip_network("224.0.0.0/4"),  # Multicast
    ip_network("240.0.0.0/4"),  # Reserved
    ip_network("255.255.255.255/32"),  # Broadcast
    ip_network("fe80::/10"),  # IPv6 link-local
    ip_network("::ffff:0:0/96"),  # IPv4-mapped IPv6 catch-all
    ip_network("::/128"),  # IPv6 unspecified
    ip_network("64:ff9b::/96"),  # NAT64 well-known prefix
    ip_network("2002::/16"),  # 6to4 (wraps RFC1918 and other reserved ranges)
]

BLOCKED_NETWORKS = LOCAL_DEV_OVERRIDABLE_NETWORKS + ALWAYS_BLOCKED_NETWORKS


def allow_private_network_urls() -> bool:
    """Return whether local/private URL fetching is explicitly allowed for local dev."""
    return os.getenv("SCRAPER_ALLOW_PRIVATE_NETWORK_URLS", "").strip().lower() in _TRUE_VALUES


def resolve_host_ips(hostname: str) -> list[str]:
    """Resolve *hostname* to IP addresses (IPv4/IPv6) via DNS."""
    addresses: list[str] = []
    for info in socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP):
        addr = str(info[4][0])
        if addr not in addresses:
            addresses.append(addr)
    return addresses


def is_ip_blocked(ip_str: str, *, allow_private_networks: bool | None = None) -> bool:
    """Return ``True`` if *ip_str* falls within any :data:`BLOCKED_NETWORKS`.

    IPv4-mapped IPv6 addresses (e.g. ``::ffff:127.0.0.1``) are unwrapped to
    their IPv4 form before the check so they cannot bypass IPv4 blocked ranges.
    """
    if allow_private_networks is None:
        allow_private_networks = allow_private_network_urls()
    try:
        ip_obj = ip_address(ip_str)
    except ValueError:
        # Unparseable address -- treat as blocked for safety.
        return True
    # Unwrap IPv4-mapped IPv6 (::ffff:a.b.c.d) so IPv4 blocked ranges apply.
    if ip_obj.version == 6 and ip_obj.ipv4_mapped is not None:
        ip_obj = ip_obj.ipv4_mapped
    if any(ip_obj in network for network in ALWAYS_BLOCKED_NETWORKS):
        return True
    if allow_private_networks:
        return False
    return any(ip_obj in network for network in LOCAL_DEV_OVERRIDABLE_NETWORKS)


def is_url_safe(url: str, *, allow_private_networks: bool | None = None) -> tuple[bool, str | None]:
    """Check whether *url* resolves to a public (non-internal) IP.

    Returns ``(True, None)`` when safe, or ``(False, reason)`` when blocked.
    Performs DNS resolution and checks all resolved addresses against
    :data:`BLOCKED_NETWORKS`.

    For direct httpx callers, pair with :func:`make_safe_async_client` /
    :func:`make_safe_sync_client` to close the DNS-rebinding TOCTOU window.
    This function alone is sufficient for Playwright route interception, where
    connection-time enforcement is not available.
    """
    if allow_private_networks is None:
        allow_private_networks = allow_private_network_urls()

    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
    except Exception:
        return False, "Malformed URL"

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False, f"URL scheme '{parsed.scheme}' is not allowed"

    if not hostname:
        return False, "Hostname is empty"

    hostname_lower = hostname.lower()
    if hostname_lower in ("localhost", "localhost.localdomain"):
        if allow_private_networks:
            return True, None
        return False, "Localhost is not allowed"

    # Fast path for IP literals -- skip DNS resolution.
    try:
        ip_obj = ip_address(hostname)
        if is_ip_blocked(str(ip_obj), allow_private_networks=allow_private_networks):
            return False, f"Private or reserved IP address: {hostname}"
        return True, None
    except ValueError:
        pass  # Not an IP literal; fall through to DNS resolution.

    try:
        resolved_ips = resolve_host_ips(hostname)
    except (socket.gaierror, OSError):
        return False, f"DNS resolution failed for {hostname}"

    if not resolved_ips:
        return False, f"No DNS records found for {hostname}"

    for resolved in resolved_ips:
        if is_ip_blocked(resolved, allow_private_networks=allow_private_networks):
            return False, f"Hostname resolves to blocked address: {resolved}"

    return True, None


def _pin_request(request: httpx.Request, hostname: str, results: list[Any]) -> httpx.Request:
    """Build a new request with the URL host rewritten to the resolved IP."""
    resolved_ip: str = results[0][4][0]
    ip_for_url = f"[{resolved_ip}]" if ":" in resolved_ip else resolved_ip

    new_url = request.url.copy_with(host=ip_for_url)

    # Restore Host header to the original hostname (httpx auto-sets it from the URL).
    original_host = request.headers.get("host", hostname)
    new_raw_headers = [(k, v) for k, v in request.headers.raw if k.lower() != b"host"]
    new_raw_headers.append((b"host", original_host.encode("latin-1")))

    extensions = dict(request.extensions)
    if request.url.scheme == "https":
        extensions["sni_hostname"] = hostname.encode("ascii")

    return httpx.Request(
        method=request.method,
        url=new_url,
        headers=new_raw_headers,
        stream=request.stream,
        extensions=extensions,
    )


def _check_results(hostname: str, results: list[Any]) -> None:
    """Raise ConnectError if any resolved IP is in a blocked range."""
    if not results:
        raise httpx.ConnectError(f"DNS resolution failed for {hostname}: no results")
    for _family, _type, _proto, _canonname, sockaddr in results:
        ip: str = sockaddr[0]
        if is_ip_blocked(ip):
            raise httpx.ConnectError(f"SSRF blocked: {ip} is in a reserved range")


class SafeAsyncTransport(httpx.AsyncHTTPTransport):
    """Async httpx transport that pins the resolved IP at connect time.

    Resolves DNS before connecting, validates every returned IP against
    BLOCKED_NETWORKS, then rewrites the request URL to the raw IP so
    httpcore makes no further DNS queries.  This closes the DNS-rebinding
    TOCTOU window that exists when is_url_safe() is used as a preflight-only
    check (TTL=0 → public IP on preflight, private IP at connect time).

    HTTPS connections retain correct TLS behaviour: sni_hostname is set in
    request extensions so certificate validation and HTTP/2 ALPN negotiation
    use the original hostname, not the substituted IP.

    Playwright-based providers use route interception only; that path does not
    benefit from connection-time enforcement (browser DNS is opaque to us).
    """

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme not in ("http", "https"):
            raise httpx.ConnectError(f"Blocked scheme: {request.url.scheme!r}")

        hostname = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)

        loop = asyncio.get_running_loop()
        try:
            results: list[Any] = await loop.run_in_executor(
                None,
                lambda: socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM),
            )
        except socket.gaierror as exc:
            raise httpx.ConnectError(f"DNS resolution failed for {hostname}: {exc}") from exc

        _check_results(hostname, results)
        return await super().handle_async_request(_pin_request(request, hostname, results))


class SafeSyncTransport(httpx.HTTPTransport):
    """Sync counterpart of SafeAsyncTransport — same IP-pinning invariants."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.scheme not in ("http", "https"):
            raise httpx.ConnectError(f"Blocked scheme: {request.url.scheme!r}")

        hostname = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)

        try:
            results: list[Any] = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise httpx.ConnectError(f"DNS resolution failed for {hostname}: {exc}") from exc

        _check_results(hostname, results)
        return super().handle_request(_pin_request(request, hostname, results))


def make_safe_async_client(**kwargs: Any) -> httpx.AsyncClient:
    """Return an AsyncClient backed by SafeAsyncTransport."""
    return httpx.AsyncClient(transport=SafeAsyncTransport(), **kwargs)


def make_safe_sync_client(**kwargs: Any) -> httpx.Client:
    """Return a sync Client backed by SafeSyncTransport."""
    return httpx.Client(transport=SafeSyncTransport(), **kwargs)
