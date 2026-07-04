"""Tests for the git-mirror SSRF guards (app.core.git_url_safety).

All cases use literal IPs so getaddrinfo resolves offline (no DNS / network).
"""

from __future__ import annotations

import pytest

from app.core.git_url_safety import (
    assert_resolved_public_host,
    assert_safe_git_url,
    extract_git_host,
    is_github_host,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Real GitHub hosts -> token may be embedded.
        ("https://github.com/owner/repo.git", True),
        ("https://www.github.com/owner/repo.git", True),
        ("git@github.com:owner/repo.git", True),
        ("https://GitHub.com/owner/repo.git", True),  # case-insensitive
        # Gist host -> token may be embedded (GitHub-owned).
        ("https://gist.github.com/abc123def456.git", True),
        ("git@gist.github.com:abc123def456.git", True),
        # Credential-exfiltration bypasses -> must be rejected (token withheld).
        ("https://github.com@attacker.com/owner/repo.git", False),  # userinfo trick
        ("https://github.com.attacker.com/owner/repo.git", False),  # lookalike suffix
        ("https://attacker.com/github.com/repo.git", False),  # path, not host
        ("https://notgithub.com/owner/repo.git", False),
        ("https://api.github.com/owner/repo.git", False),  # only github.com / www
        # gist.github.com lookalikes -> must be rejected (token withheld).
        ("https://gist.github.com.evil.com/abc.git", False),  # lookalike suffix
        ("https://gist.github.com@evil.com/abc.git", False),  # userinfo trick
        ("https://notgist.github.com/abc.git", False),  # different subdomain
        ("not a url", False),
    ],
)
def test_is_github_host_blocks_token_exfiltration(url: str, expected: bool) -> None:
    """is_github_host must classify only the *real* host as GitHub, so a token is
    never embedded for a userinfo/lookalike host (audit: CRITICAL cred exfil)."""
    assert is_github_host(url) is expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/repo.git", "github.com"),
        ("https://x-access-token:tok@github.com/owner/repo.git", "github.com"),
        ("http://EXAMPLE.com/x", "example.com"),
        ("git@github.com:owner/repo.git", "github.com"),
        ("ssh://git@host.example:2222/x", "host.example"),
        ("not a url", None),
    ],
)
def test_extract_git_host(url: str, expected: str | None) -> None:
    assert extract_git_host(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://172.16.0.1/x",
        "http://[::1]/x",
        "http://0.0.0.0/x",
        "http://localhost/x",
        "git@localhost:x/y.git",
    ],
)
def test_assert_safe_git_url_rejects_non_public_literals(url: str) -> None:
    with pytest.raises(ValueError):
        assert_safe_git_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo.git",  # public hostname (no DNS at this layer)
        "https://8.8.8.8/x",  # public literal
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
    ],
)
def test_assert_safe_git_url_allows_public(url: str) -> None:
    assert_safe_git_url(url)  # must not raise


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c 'touch /tmp/pwned'",  # git remote-helper transport (RCE)
        "fd::7",  # git remote-helper transport reading an inherited fd
        "file:///etc/passwd",  # file scheme, no clone_url legitimately needs it
        "file://localhost/etc/passwd",  # file scheme with an explicit host
        "ext::sh -c 'evil' https://github.com/owner/repo.git",  # trailing decoy
    ],
)
def test_assert_safe_git_url_rejects_disallowed_transports(url: str) -> None:
    """audit finding: git transport-scheme injection (ext::/fd::/file://) must be
    rejected before any host check runs -- previously the scp-like fallback in
    extract_git_host returned the literal transport keyword as a 'host', which
    is neither a blocked host nor an IP, so it slipped past assert_safe_git_url
    and let git itself interpret the ``scheme::`` remote-helper prefix."""
    with pytest.raises(ValueError):
        assert_safe_git_url(url)


def test_assert_safe_git_url_allows_bracketed_ipv6_literal_host() -> None:
    """A bracketed IPv6 literal (``[::1]``) must not be mistaken for the
    remote-helper ``scheme::`` syntax; it is still rejected here because
    ::1 is loopback, but for the right reason (non-public address)."""
    with pytest.raises(ValueError, match="non-public"):
        assert_safe_git_url("http://[::1]/x")


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "169.254.169.254", "10.0.0.5", "192.168.1.1", "::1", "localhost"],
)
def test_assert_resolved_public_host_rejects_non_public(host: str) -> None:
    with pytest.raises(ValueError):
        assert_resolved_public_host(host)


def test_assert_resolved_public_host_allows_public_literal() -> None:
    assert_resolved_public_host("8.8.8.8")  # must not raise


def test_assert_resolved_public_host_blocks_rebinding(monkeypatch: pytest.MonkeyPatch) -> None:
    """A public-looking hostname that resolves to a private address is rejected."""

    def fake_getaddrinfo(host: str, *_args: object, **_kwargs: object) -> list:
        return [(2, 1, 6, "", ("10.1.2.3", 0))]

    monkeypatch.setattr("app.core.git_url_safety.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError):
        assert_resolved_public_host("evil.example.com")
