"""SSRF guards for git mirror clone URLs.

The git-backup subsystem clones arbitrary, user-supplied URLs from the worker
process, which typically has network reachability to internal services and the
cloud metadata endpoint (169.254.169.254). Without a host allowlist these
helpers reject URLs that target private, loopback, link-local, or otherwise
non-public hosts.

Two layers are provided:

- ``assert_safe_git_url`` -- cheap, non-blocking syntactic check (no DNS). Safe
  to call inside request validation / a Telegram handler. Catches literal-IP and
  localhost targets immediately.
- ``assert_resolved_public_host`` -- authoritative check that resolves the host
  via ``getaddrinfo`` and rejects if ANY resolved address is non-public. Performs
  blocking DNS, so it must run in a worker / thread, not on the event loop. This
  is the real enforcement point: it also covers DB-sourced and config rows that
  never pass through the input validators, and narrows the DNS-rebinding window
  by resolving immediately before the clone.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

__all__ = [
    "assert_resolved_public_host",
    "assert_safe_git_url",
    "extract_git_host",
    "is_github_host",
]

# Hostnames that are never legitimate clone targets regardless of resolution.
_BLOCKED_HOSTNAMES = frozenset(
    {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"}
)

# Exact hostnames for which a GitHub access token may be embedded in a clone
# URL. Anything else -- lookalikes like ``github.com.evil.com`` or userinfo
# tricks like ``github.com@evil.com`` -- must NOT receive the token, because
# ``extract_git_host`` resolves those to their true (non-GitHub) host.
# ``gist.github.com`` is GitHub-owned and is used for gist clone URLs of the
# form ``https://gist.github.com/<id>.git``.
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com", "gist.github.com"})


def is_github_host(url: str) -> bool:
    """Return True only if the URL's real parsed host is exactly GitHub.

    Uses :func:`extract_git_host`, so credential-injection (``github.com@evil``)
    and lookalike (``github.com.evil.com``) URLs resolve to their true host and
    are correctly rejected. This is the gate that prevents leaking a GitHub
    access token to an attacker-controlled host.
    """
    host = extract_git_host(url)
    return host in _GITHUB_HOSTS if host else False


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if the address is not a routable public address."""
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def extract_git_host(url: str) -> str | None:
    """Return the lowercased hostname from a git clone URL, or None if unparseable.

    Handles URL-scheme forms (https/http/git/ssh, including embedded ``user@``
    credentials) and scp-like syntax (``[user@]host:path``).
    """
    stripped = url.strip()
    if "://" in stripped:
        host = urlparse(stripped).hostname
        return host.lower() if host else None
    # scp-like syntax: [user@]host:path
    if "@" in stripped:
        stripped = stripped.split("@", 1)[1]
    if ":" in stripped:
        host = stripped.split(":", 1)[0]
        return host.lower() or None
    return None


def assert_safe_git_url(url: str) -> None:
    """Syntactic SSRF guard (no DNS). Raise ValueError for an unsafe literal host.

    Rejects blocked hostnames and literal IPs in non-public ranges. Hostnames
    that are not literal IPs are deferred to ``assert_resolved_public_host`` at
    clone time. Safe to call on the event loop (no network I/O).
    """
    host = extract_git_host(url)
    if host is None:
        msg = "clone_url has no parseable host"
        raise ValueError(msg)
    if host in _BLOCKED_HOSTNAMES:
        msg = "clone_url host is not allowed"
        raise ValueError(msg)
    candidate = host.strip("[]")  # tolerate bracketed IPv6 literals
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        return  # not a literal IP -> resolution-time check decides
    if _ip_is_blocked(ip):
        msg = "clone_url targets a non-public address"
        raise ValueError(msg)


def assert_resolved_public_host(host: str) -> None:
    """Authoritative SSRF guard. Resolve ``host`` and raise ValueError if ANY
    resolved address is non-public.

    Performs blocking DNS via ``getaddrinfo`` -- call from a worker / thread, not
    the event loop.
    """
    if host in _BLOCKED_HOSTNAMES:
        msg = f"host {host!r} is not allowed"
        raise ValueError(msg)
    candidate = host.strip("[]")
    try:
        infos = socket.getaddrinfo(candidate, None)
    except socket.gaierror as exc:
        msg = f"could not resolve host {host!r}"
        raise ValueError(msg) from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if _ip_is_blocked(ip):
            msg = f"host {host!r} resolves to a non-public address"
            raise ValueError(msg)
