"""URL helpers for Threads and Instagram surfaces."""

from __future__ import annotations

import re
from urllib.parse import ParseResult, urlparse

from app.core.urls.normalization import normalize_url

_THREADS_HOSTS = frozenset({"threads.net", "www.threads.net"})
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com"})
_THREADS_POST_RE = re.compile(r"^/@[^/]+/post/([^/?#]+)", re.IGNORECASE)
_THREADS_T_RE = re.compile(r"^/t/([^/?#]+)", re.IGNORECASE)
_INSTAGRAM_POST_RE = re.compile(r"^/p/([^/?#]+)", re.IGNORECASE)
_INSTAGRAM_REEL_RE = re.compile(r"^/(?:reel|reels|tv)/([^/?#]+)", re.IGNORECASE)


def is_threads_url(url: str) -> bool:
    """Return whether the URL targets a Threads post surface."""

    return extract_threads_post_id(url) is not None


def extract_threads_post_id(url: str) -> str | None:
    """Extract a stable Threads post ID from a supported Threads URL."""

    parsed = _parse_meta_url(url)
    if parsed is None or parsed.hostname not in _THREADS_HOSTS:
        return None
    for pattern in (_THREADS_POST_RE, _THREADS_T_RE):
        match = pattern.match(parsed.path)
        if match:
            return match.group(1)
    return None


def is_instagram_url(url: str) -> bool:
    """Return whether the URL targets a supported Instagram surface."""

    return is_instagram_post_url(url) or is_instagram_reel_url(url)


def is_instagram_post_url(url: str) -> bool:
    """Return whether the URL is an Instagram post/carousel URL."""

    parsed = _parse_meta_url(url)
    if parsed is None or parsed.hostname not in _INSTAGRAM_HOSTS:
        return False
    return _INSTAGRAM_POST_RE.match(parsed.path) is not None


def is_instagram_reel_url(url: str) -> bool:
    """Return whether the URL is an Instagram reel-style URL."""

    parsed = _parse_meta_url(url)
    if parsed is None or parsed.hostname not in _INSTAGRAM_HOSTS:
        return False
    return _INSTAGRAM_REEL_RE.match(parsed.path) is not None


def extract_instagram_shortcode(url: str) -> str | None:
    """Extract the canonical shortcode from a supported Instagram URL."""

    parsed = _parse_meta_url(url)
    if parsed is None or parsed.hostname not in _INSTAGRAM_HOSTS:
        return None
    for pattern in (_INSTAGRAM_POST_RE, _INSTAGRAM_REEL_RE):
        match = pattern.match(parsed.path)
        if match:
            return match.group(1)
    return None


def _parse_meta_url(url: str) -> ParseResult | None:
    try:
        normalized = normalize_url(url)
    except ValueError:
        candidate = url.strip()
        if not candidate:
            return None
        if "://" not in candidate:
            candidate = f"https://{candidate}"
        normalized = candidate

    try:
        parsed = urlparse(normalized)
    except ValueError:
        return None
    hostname = parsed.hostname.lower() if parsed.hostname else None
    if hostname is None:
        return None
    return parsed._replace(netloc=hostname)


__all__ = [
    "extract_instagram_shortcode",
    "extract_threads_post_id",
    "is_instagram_post_url",
    "is_instagram_reel_url",
    "is_instagram_url",
    "is_threads_url",
]
