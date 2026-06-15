"""Runtime tuning helpers for scraper providers."""

from __future__ import annotations

from urllib.parse import urlparse

from app.config.scraper import profile_retry_budget, profile_timeout_multiplier

# Re-export so existing callers (tests, providers) keep working without changes.
__all__ = [
    "BROWSER_PROVIDERS",
    "is_js_heavy_url",
    "normalize_hosts",
    "normalize_profile",
    "profile_retry_budget",
    "profile_timeout_multiplier",
    "tuned_firecrawl_wait_for_ms",
    "tuned_provider_timeout",
]


def normalize_profile(profile: str) -> str:
    """Normalise a profile name to one of fast/balanced/robust."""
    value = profile.strip().lower()
    if value not in {"fast", "balanced", "robust"}:
        return "balanced"
    return value


# Providers used for JS-heavy URL reordering (in-process or CDP-sidecar browser
# drivers that benefit from running first on JS-heavy hosts). This is a subset
# of the chain's _BROWSER_TIER_PROVIDERS: scrapegraph_ai is in the tier for
# racing/grouping purposes but does not benefit from the JS-heavy reorder
# (it is an LLM fallback, not a browser driver).
# cloakbrowser IS a browser driver and benefits from the reorder, so it is
# included here alongside playwright and crawlee.
BROWSER_PROVIDERS: frozenset[str] = frozenset({"playwright", "crawlee", "cloakbrowser"})


def normalize_hosts(hosts: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    normalized = {str(host).strip().lower() for host in hosts if str(host).strip()}
    return tuple(sorted(normalized))


def is_js_heavy_url(url: str, js_heavy_hosts: tuple[str, ...] | list[str]) -> bool:
    host = _extract_host(url)
    if not host:
        return False
    allowed = set(normalize_hosts(tuple(js_heavy_hosts)))
    if host in allowed:
        return True
    return any(host.endswith(f".{suffix}") for suffix in allowed)


def tuned_provider_timeout(
    *,
    base_timeout_sec: float,
    profile: str,
    provider: str,
    url: str,
    js_heavy_hosts: tuple[str, ...] | list[str],
) -> float:
    timeout = max(1.0, float(base_timeout_sec))
    timeout *= profile_timeout_multiplier(profile)

    if is_js_heavy_url(url, js_heavy_hosts):
        if provider == "scrapling":
            timeout *= 0.8
        elif provider in {"playwright", "crawlee"}:
            timeout *= 1.25

    return max(1.0, timeout)


def tuned_firecrawl_wait_for_ms(
    *,
    base_wait_for_ms: int,
    url: str,
    js_heavy_hosts: tuple[str, ...] | list[str],
) -> int:
    wait_for_ms = max(0, int(base_wait_for_ms))
    if wait_for_ms <= 0:
        return 0
    if is_js_heavy_url(url, js_heavy_hosts):
        return min(10_000, int(wait_for_ms * 1.3))
    return wait_for_ms


def _extract_host(url: str) -> str | None:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return None
    host = (parsed.hostname or "").strip().lower()
    return host or None
