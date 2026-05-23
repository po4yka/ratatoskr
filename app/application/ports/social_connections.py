"""Ports for encrypted social connection persistence."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime


SUPPORTED_SOCIAL_PROVIDERS: frozenset[str] = frozenset({"x", "instagram", "threads"})


@dataclass(frozen=True, slots=True)
class SocialConnectionRecord:
    """Persisted encrypted social connection snapshot."""

    id: int
    user_id: int
    provider: str
    auth_type: str
    provider_user_id: str | None
    provider_username: str | None
    encrypted_access_token: bytes | None
    encrypted_refresh_token: bytes | None
    token_scopes: list[str] | None
    access_token_expires_at: datetime | None
    refresh_token_expires_at: datetime | None
    status: str
    metadata_json: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    def without_tokens(self) -> SocialConnectionRecord:
        """Return a copy safe for JSON/log surfaces."""
        return replace(self, encrypted_access_token=None, encrypted_refresh_token=None)


@dataclass(frozen=True, slots=True)
class SocialConnectionUpsert:
    """Create/update payload for an encrypted social connection."""

    user_id: int
    provider: str
    auth_type: str
    provider_user_id: str | None = None
    provider_username: str | None = None
    encrypted_access_token: bytes | None = None
    encrypted_refresh_token: bytes | None = None
    token_scopes: list[str] | None = None
    access_token_expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None
    status: str = "active"
    metadata_json: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SocialConnectionUpdate:
    """Partial update payload for an encrypted social connection."""

    auth_type: str | None = None
    provider_user_id: str | None = None
    provider_username: str | None = None
    encrypted_access_token: bytes | None = None
    encrypted_refresh_token: bytes | None = None
    token_scopes: list[str] | None = None
    access_token_expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None
    status: str | None = None
    metadata_json: dict[str, Any] | None = None


@runtime_checkable
class SocialConnectionRepositoryPort(Protocol):
    """Persistence operations for encrypted social provider connections."""

    async def get_by_user_and_provider(
        self, user_id: int, provider: str
    ) -> SocialConnectionRecord | None:
        """Return a user's connection for a provider."""

    async def upsert_connection(self, connection: SocialConnectionUpsert) -> SocialConnectionRecord:
        """Create or replace a user's connection for a provider."""

    async def update_connection(
        self, user_id: int, provider: str, update: SocialConnectionUpdate
    ) -> SocialConnectionRecord | None:
        """Patch an existing connection."""
