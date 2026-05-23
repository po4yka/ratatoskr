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


@dataclass(frozen=True, slots=True)
class SocialAuthStateRecord:
    """Persisted social OAuth state snapshot."""

    id: int
    user_id: int
    provider: str
    state_hash: str
    encrypted_code_verifier: bytes | None
    redirect_uri: str | None
    scopes: list[str] | None
    status: str
    metadata_json: dict[str, Any] | None
    expires_at: datetime
    consumed_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SocialAuthStateCreate:
    """Create payload for a provider-neutral OAuth state."""

    user_id: int
    provider: str
    state_hash: str
    encrypted_code_verifier: bytes | None
    redirect_uri: str
    scopes: list[str]
    expires_at: datetime
    metadata_json: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SocialFetchAttemptCreate:
    """Create payload for a social provider fetch attempt."""

    user_id: int
    provider: str
    attempt_type: str
    status: str
    connection_id: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    metadata_json: dict[str, Any] | None = None


@runtime_checkable
class SocialConnectionRepositoryPort(Protocol):
    """Persistence operations for encrypted social provider connections."""

    async def get_by_user_and_provider(
        self, user_id: int, provider: str
    ) -> SocialConnectionRecord | None:
        """Return a user's connection for a provider."""

    async def list_by_user(self, user_id: int) -> list[SocialConnectionRecord]:
        """Return every stored social connection for a user."""

    async def upsert_connection(self, connection: SocialConnectionUpsert) -> SocialConnectionRecord:
        """Create or replace a user's connection for a provider."""

    async def update_connection(
        self, user_id: int, provider: str, update: SocialConnectionUpdate
    ) -> SocialConnectionRecord | None:
        """Patch an existing connection."""

    async def delete_connection(self, user_id: int, provider: str) -> bool:
        """Delete a user's connection for a provider."""

    async def create_auth_state(self, state: SocialAuthStateCreate) -> SocialAuthStateRecord:
        """Persist a new social OAuth state."""

    async def get_auth_state(self, provider: str, state_hash: str) -> SocialAuthStateRecord | None:
        """Load an OAuth state by provider and hashed state."""

    async def mark_auth_state_consumed(self, state_id: int) -> SocialAuthStateRecord | None:
        """Mark a pending OAuth state consumed exactly once."""

    async def mark_auth_state_expired(self, state_id: int) -> SocialAuthStateRecord | None:
        """Mark an OAuth state expired."""

    async def record_fetch_attempt(self, attempt: SocialFetchAttemptCreate) -> None:
        """Persist a social provider fetch attempt."""
