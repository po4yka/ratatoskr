"""Stable operator browser profile shared by AI backup login and collection."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from app.adapters.content.scraper.fingerprint import seed_for_url

if TYPE_CHECKING:
    from app.config.ai_backup import AiBackupConfig


class AiBackupBrowserProfile(TypedDict):
    fingerprint_seed: str
    timezone: str
    locale: str


def browser_profile(domain: str, config: AiBackupConfig) -> AiBackupBrowserProfile:
    """Return the pinned profile for a provider domain.

    The existing per-domain seed remains stable so stored sessions retain their
    TLS fingerprint. CloakBrowser must be restarted when locale or timezone is
    changed because those process settings are first-launch wins.
    """
    locale = config.browser_locale.strip()
    timezone = config.browser_timezone.strip()
    normalized_domain = domain.lower().strip().strip(".")
    seed = seed_for_url(f"https://{normalized_domain}")
    return {"fingerprint_seed": seed, "timezone": timezone, "locale": locale}
