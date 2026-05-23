"""Provider capability snapshots for social integrations."""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class SocialProviderCapabilities:
    provider: str
    supports_single_url_lookup: bool
    supports_owned_media_lookup: bool
    supports_public_media_lookup: bool
    supports_timeline_ingestion: bool
    supports_refresh_tokens: bool
    supported_scopes: tuple[str, ...]
    unsupported_notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["supported_scopes"] = list(self.supported_scopes)
        payload["unsupported_notes"] = list(self.unsupported_notes)
        return payload


_CAPABILITIES: dict[str, SocialProviderCapabilities] = {
    "x": SocialProviderCapabilities(
        provider="x",
        supports_single_url_lookup=True,
        supports_owned_media_lookup=False,
        supports_public_media_lookup=True,
        supports_timeline_ingestion=True,
        supports_refresh_tokens=True,
        supported_scopes=("tweet.read", "users.read", "offline.access"),
        unsupported_notes=(
            "Read-only X API access is supported for post lookup and configured timeline ingestion.",
            "Write, publish, DM, moderation, and ad scopes are intentionally unsupported.",
        ),
    ),
    "threads": SocialProviderCapabilities(
        provider="threads",
        supports_single_url_lookup=True,
        supports_owned_media_lookup=True,
        supports_public_media_lookup=True,
        supports_timeline_ingestion=True,
        supports_refresh_tokens=True,
        supported_scopes=("threads_basic",),
        unsupported_notes=(
            "Threads support is limited to read-only media lookup and the connected account's /me/threads feed.",
            "Publishing, replies, insights, and webhook behavior are intentionally unsupported.",
        ),
    ),
    "instagram": SocialProviderCapabilities(
        provider="instagram",
        supports_single_url_lookup=True,
        supports_owned_media_lookup=True,
        supports_public_media_lookup=False,
        supports_timeline_ingestion=False,
        supports_refresh_tokens=True,
        supported_scopes=("instagram_business_basic",),
        unsupported_notes=(
            "Authenticated Instagram API lookup is limited to media owned by the connected professional account.",
            "General public/private feed access, personal-account media access, publishing, comments, messaging, insights, and ads are intentionally unsupported.",
        ),
    ),
}


def get_social_provider_capabilities(provider: str) -> SocialProviderCapabilities:
    normalized = provider.strip().lower()
    try:
        return _CAPABILITIES[normalized]
    except KeyError as exc:
        raise ValueError(f"Unsupported social provider: {provider}") from exc


def list_social_provider_capabilities() -> tuple[SocialProviderCapabilities, ...]:
    return tuple(_CAPABILITIES[provider] for provider in sorted(_CAPABILITIES))


def default_social_scopes() -> dict[str, list[str]]:
    return {
        provider: list(capabilities.supported_scopes)
        for provider, capabilities in _CAPABILITIES.items()
    }


def unsupported_social_scopes(provider: str, scopes: list[str]) -> list[str]:
    supported = set(get_social_provider_capabilities(provider).supported_scopes)
    return [scope for scope in scopes if scope not in supported]
