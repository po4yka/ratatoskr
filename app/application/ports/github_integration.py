"""Port: GitHub integration persistence.

Defines the repository interface and domain value types the application layer
uses for persisting GitHub integration data. Infrastructure adapters implement
these against SQLAlchemy + the ORM models.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime


class GitHubAuthMethod(enum.StrEnum):
    """Authentication method used to connect the GitHub integration."""

    PAT = "pat"
    OAUTH_DEVICE = "oauth_device"


class GitHubIntegrationStatus(enum.StrEnum):
    """Current health status of the GitHub integration token."""

    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    REVOKED = "revoked"


@dataclass(frozen=True)
class GitHubIntegrationRecord:
    """Domain snapshot of a persisted GitHub integration row.

    Returned by the repository port so the use case never touches ORM models.
    """

    id: int
    user_id: int
    auth_method: GitHubAuthMethod
    encrypted_token: bytes
    token_scopes: str | None
    github_login: str | None
    github_user_id: int | None
    status: GitHubIntegrationStatus
    last_synced_at: datetime | None


@dataclass(frozen=True)
class GitHubIntegrationUpsert:
    """Payload for creating or updating a GitHub integration row."""

    user_id: int
    auth_method: GitHubAuthMethod
    encrypted_token: bytes
    token_scopes: str
    github_login: str
    github_user_id: int
    status: GitHubIntegrationStatus


@runtime_checkable
class GitHubIntegrationRepositoryPort(Protocol):
    """Persistence operations for GitHub user integrations."""

    async def get_by_user_id(self, user_id: int) -> GitHubIntegrationRecord | None:
        """Return the integration row for *user_id*, or None when absent."""
        ...

    async def upsert(self, payload: GitHubIntegrationUpsert) -> GitHubIntegrationRecord:
        """Create or update the integration for payload.user_id.

        Returns the persisted record with all server-populated fields.
        """
        ...

    async def delete_by_user_id(self, user_id: int) -> None:
        """Delete the integration row for *user_id* (no-op when absent)."""
        ...

    async def count_repositories(self, user_id: int) -> int:
        """Return the number of Repository rows owned by *user_id*."""
        ...
