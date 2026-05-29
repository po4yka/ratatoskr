"""Tests for the git-mirror SSRF guards (app.core.git_url_safety).

All cases use literal IPs so getaddrinfo resolves offline (no DNS / network).
"""

from __future__ import annotations

import pytest

from app.core.git_url_safety import (
    assert_resolved_public_host,
    assert_safe_git_url,
    extract_git_host,
)


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
    ],
)
def test_assert_safe_git_url_allows_public(url: str) -> None:
    assert_safe_git_url(url)  # must not raise


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
