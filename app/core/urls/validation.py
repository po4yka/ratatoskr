from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Generator

logger = get_logger(__name__)

_dns_cache: contextvars.ContextVar[dict[str, list[Any]] | None] = contextvars.ContextVar(
    "_dns_cache", default=None
)

_DANGEROUS_URL_SUBSTRINGS: tuple[str, ...] = (
    "<",
    ">",
    '"',
    "'",
    "script",
    "javascript:",
    "data:",
)

_ALLOWED_SCHEMES: frozenset[str] = frozenset(["http", "https"])

_DANGEROUS_SCHEMES: frozenset[str] = frozenset(
    [
        "file",
        "ftp",
        "ftps",
        "javascript",
        "data",
        "vbscript",
        "about",
        "blob",
        "filesystem",
        "ws",
        "wss",
        "mailto",
        "tel",
        "sms",
        "ssh",
        "sftp",
        "telnet",
        "gopher",
        "ldap",
        "ldaps",
    ]
)


@contextmanager
def dns_cache_scope() -> Generator[None]:
    """Enable DNS resolution caching for the duration of this scope."""
    token = _dns_cache.set({})
    try:
        yield
    finally:
        _dns_cache.reset(token)


def validate_url_input(url: str) -> None:
    """Validate URL input for security."""
    _validate_url_input_basics(url)

    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        hostname = parsed.hostname or parsed.netloc
        if hostname:
            _validate_hostname_security(hostname)
    except ValueError:
        raise
    except Exception as exc:
        logger.debug(
            "url_validation_parse_warning",
            extra={"url": url[:100], "error": str(exc)},
        )


def _validate_url_input_basics(url: str) -> None:
    if not url:
        msg = "URL cannot be empty"
        raise ValueError(msg)
    if not isinstance(url, str):
        msg = "URL must be a string"
        raise ValueError(msg)
    if len(url) > 2048:
        msg = "URL too long"
        raise ValueError(msg)

    url_lower = url.lower()
    if any(needle in url_lower for needle in _DANGEROUS_URL_SUBSTRINGS):
        msg = "URL contains potentially dangerous content"
        raise ValueError(msg)
    for dangerous_scheme in _DANGEROUS_SCHEMES:
        if url_lower.startswith(f"{dangerous_scheme}:"):
            msg = f"URL scheme '{dangerous_scheme}' is not allowed"
            raise ValueError(msg)
    if "\x00" in url:
        msg = "URL contains null bytes"
        raise ValueError(msg)
    if any(char in ("\t", "\n", "\r") for char in url):
        msg = "URL contains invalid whitespace characters"
        raise ValueError(msg)
    if any(ord(char) < 32 for char in url):
        msg = "URL contains control characters"
        raise ValueError(msg)


def _validate_hostname_security(hostname: str) -> None:
    import ipaddress

    from app.security.ssrf import allow_private_network_urls, is_ip_blocked

    hostname_lower = hostname.lower()
    if hostname_lower in ("localhost", "localhost.localdomain"):
        if allow_private_network_urls():
            return
        msg = "Localhost access not allowed"
        raise ValueError(msg)

    try:
        ip_obj = ipaddress.ip_address(hostname)
    except ValueError:
        ip_obj = None

    if ip_obj is not None:
        if is_ip_blocked(str(ip_obj)):
            msg = f"Blocked IP address: {ip_obj}"
            raise ValueError(msg)
        return

    _validate_suspicious_domain_pattern(hostname_lower)

    resolved_ips = _resolve_hostname_to_addrs(hostname, hostname_lower)
    for info in resolved_ips:
        addr_str = str(info[4][0])
        if is_ip_blocked(addr_str):
            msg = f"Hostname resolves to blocked IP address: {addr_str}"
            raise ValueError(msg)


def _validate_suspicious_domain_pattern(hostname_lower: str) -> None:
    suspicious_patterns = (".local", ".internal", ".lan", ".corp", ".test", ".invalid")
    for pattern in suspicious_patterns:
        if hostname_lower.endswith(pattern):
            msg = f"Suspicious domain pattern: {pattern}"
            raise ValueError(msg)


def _resolve_hostname_to_addrs(hostname: str, hostname_lower: str) -> list[Any]:
    """Resolve hostname, respecting the per-task DNS cache when active."""
    import socket

    cache = _dns_cache.get()
    if cache is not None and hostname_lower in cache:
        return cache[hostname_lower]

    try:
        resolved = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError:
        resolved = []
    if cache is not None:
        cache[hostname_lower] = resolved
    return resolved


__all__ = [
    "dns_cache_scope",
    "validate_url_input",
]
