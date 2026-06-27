"""Browser fingerprint primitives shared by the CloakBrowser scraper and auth context.

Extracted from ``cloakbrowser_provider`` so both the anonymous scraper path and the
authenticated-context module can import public names without touching private internals.

Public surface: ``DESKTOP_UA``, ``MOBILE_UA``, ``LOCALE_POOL``, ``seed_for_url``,
``locale_for_seed``, ``build_cdp_url``.
"""

from __future__ import annotations

import hashlib
from urllib.parse import quote, urlsplit

from app.core.url_utils import extract_domain

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 11; Pixel 5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# Picked deterministically per-domain so that a host always sees the same
# (timezone, locale) — same as a real returning user would.
LOCALE_POOL: tuple[tuple[str, str], ...] = (
    ("UTC", "en-US"),
    ("Europe/Berlin", "de-DE"),
    ("Asia/Tokyo", "ja-JP"),
    ("America/Sao_Paulo", "pt-BR"),
)


def seed_for_url(url: str) -> str:
    """Deterministic 12-hex-char fingerprint seed keyed on the registrable domain."""
    # extract_domain is the project-wide normalizer; falls back to the raw
    # netloc if parsing fails. The empty-string case is fine — sha1("") is
    # still a valid hex digest.
    domain = (extract_domain(url) or urlsplit(url).netloc or "").lower()
    return hashlib.sha1(domain.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def locale_for_seed(seed: str) -> tuple[str, str]:
    return LOCALE_POOL[int(seed, 16) % len(LOCALE_POOL)]


def build_cdp_url(
    endpoint_url: str, seed: str, timezone: str, locale: str, *, proxy: str = ""
) -> str:
    """Construct a cloakserve CDP endpoint URL for a fingerprint seed."""
    params = [
        f"fingerprint={seed}",
        f"timezone={quote(timezone, safe='')}",
        f"locale={quote(locale, safe='')}",
    ]
    if proxy:
        params.append(f"proxy={quote(proxy, safe='')}")
    return f"{endpoint_url.rstrip('/')}?{'&'.join(params)}"
