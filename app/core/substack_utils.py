"""Pure Substack URL resolution utilities.

These helpers have no external dependencies and belong in ``app/core`` so they
can be used by any layer (Telegram command handlers, RSS adapter, etc.) without
creating cross-adapter import cycles.
"""

from __future__ import annotations

from urllib.parse import urlparse


def is_substack_url(url: str) -> bool:
    """Check if a URL belongs to a Substack publication."""
    try:
        parsed = urlparse(url if "://" in url else f"https://{url}")
        host = (parsed.hostname or "").lower()
        return host.endswith(".substack.com") or host == "substack.com"
    except Exception:
        return False


def resolve_substack_feed_url(input_text: str) -> str:
    """Resolve a Substack publication name or URL to its RSS feed URL.

    Handles:
    - "platformer" -> "https://platformer.substack.com/feed"
    - "https://platformer.substack.com" -> "https://platformer.substack.com/feed"
    - "https://platformer.substack.com/p/some-article" -> "https://platformer.substack.com/feed"
    - "https://www.custom-domain.com" -> "https://www.custom-domain.com/feed"
    """
    text = input_text.strip()

    # Bare word (no dots, no slashes) -> treat as Substack publication name
    if "/" not in text and "." not in text:
        return f"https://{text}.substack.com/feed"

    # Ensure scheme
    url = text if "://" in text else f"https://{text}"

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if is_substack_url(url):
        # Always use the base feed URL for Substack domains
        return f"{parsed.scheme}://{host}/feed"

    # Custom domain: append /feed if not already present
    if parsed.path.rstrip("/") == "/feed":
        return url.rstrip("/")

    return f"{parsed.scheme}://{host}/feed"
