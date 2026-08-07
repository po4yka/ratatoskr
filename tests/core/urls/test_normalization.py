from __future__ import annotations

import pytest

from app.core.urls import normalization

# These tests used to stub out `normalization.validate_url_input` so that
# normalizing a URL would not perform a live DNS lookup. `normalize_url` now
# validates syntactically only (see its docstring), so the stub is gone and
# every case below exercises the real validation path.


def test_normalize_url_normalizes_components() -> None:
    normalized = normalization.normalize_url("HTTPS://Example.COM/Path/?b=2&utm_source=x&a=1#frag")

    assert normalized == "https://example.com/Path?a=1&b=2"


def test_normalize_url_reencodes_paths_and_preserves_blank_values() -> None:
    normalized = normalization.normalize_url(
        "https://Example.COM/hello world/?b=&utm_campaign=x&a=1"
    )

    assert normalized == "https://example.com/hello%20world?a=1&b="


def test_normalize_url_handles_missing_scheme() -> None:
    normalized = normalization.normalize_url("Example.com/Path?B=2&A=1")

    assert normalized == "http://example.com/Path?A=1&B=2"


def test_normalize_url_rejects_credential_injection() -> None:
    with pytest.raises(ValueError, match="hostname contains suspicious characters"):
        normalization.normalize_url("https://user:pass@example.com/path")


def test_compute_dedupe_hash_canonicalizes_twitter_status() -> None:
    first = normalization.compute_dedupe_hash("https://x.com/user/status/123?s=20&t=AAA")
    second = normalization.compute_dedupe_hash("https://x.com/i/web/status/123?utm_source=twitter")

    assert first == second


def test_normalize_url_double_encoded_path_matches_single_encoded() -> None:
    """Double-encoded %25xx and once-encoded %xx for the same character must normalize identically."""
    double_encoded = normalization.normalize_url("https://example.com/foo%2520bar")
    single_encoded = normalization.normalize_url("https://example.com/foo%20bar")

    assert double_encoded == single_encoded


def test_normalize_url_collapses_double_slashes() -> None:
    """Consecutive slashes in the path must be collapsed to a single slash."""
    normalized = normalization.normalize_url("https://example.com/a//b/")

    from urllib.parse import urlparse

    assert normalized.endswith("/a/b")
    assert "//" not in urlparse(normalized).path


def test_normalize_url_idempotent_on_various_inputs() -> None:
    """normalize_url(normalize_url(x)) == normalize_url(x) for multiple inputs."""
    inputs = [
        "https://example.com/foo%2520bar",
        "https://example.com/a//b/",
        "HTTPS://Example.COM/Path/?b=2&utm_source=x&a=1#frag",
        "https://example.com/hello world/?a=1",
        "https://example.com/foo%20bar?q=1",
    ]
    for url in inputs:
        first = normalization.normalize_url(url)
        second = normalization.normalize_url(first)
        assert first == second, f"Not idempotent for: {url!r}"


def test_normalize_url_still_rejects_blocked_hosts_without_resolving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dropping DNS from normalization must not drop the checks that need no DNS.

    Fails the test outright if normalization resolves anything: the whole point
    of the syntactic split is that this path performs no network I/O.
    """
    import socket

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("normalize_url must not resolve hostnames")

    monkeypatch.setattr(socket, "getaddrinfo", _explode)

    for url, match in [
        ("http://localhost/admin", "Localhost access not allowed"),
        ("http://169.254.169.254/latest/meta-data/", "Blocked IP address"),
        ("https://service.internal/path", "Suspicious domain pattern"),
        ("file:///etc/passwd", "scheme 'file' is not allowed"),
    ]:
        with pytest.raises(ValueError, match=match):
            normalization.normalize_url(url)

    assert normalization.normalize_url("https://example.com/ok") == "https://example.com/ok"
