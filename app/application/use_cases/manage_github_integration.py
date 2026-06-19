"""Use case: manage a user's GitHub integration (PAT / OAuth Device Flow)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.application.exceptions.github import (
    GitHubAuthError,
    InsufficientScopeError,
    InvalidGitHubTokenError,
)
from app.application.ports.github_integration import (
    GitHubAuthMethod,
    GitHubIntegrationRecord,
    GitHubIntegrationRepositoryPort,
    GitHubIntegrationStatus,
    GitHubIntegrationUpsert,
)
from app.application.use_cases._tracing import use_case_span
from app.core.logging_utils import get_logger
from app.security.token_crypto import encrypt_token

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from app.application.ports.github_gateway import GitHubGateway

logger = get_logger(__name__)

_REQUIRED_SCOPES: frozenset[str] = frozenset({"read:user", "repo"})
_KNOWN_SAFE_SCOPES: frozenset[str] = frozenset(
    {"read:user", "user:email", "repo", "public_repo", "read:org", "gist", "notifications"}
)
_OVERBROAD_SCOPES: dict[str, str] = {
    "admin:org": "token has org admin access — consider a narrower token",
    "admin:repo_hook": "token has webhook admin access — consider a narrower token",
    "delete_repo": "token can delete repositories — consider a narrower token",
    "write:packages": "token can publish packages — consider a narrower token",
    "admin:gpg_key": "token has GPG key admin access — consider a narrower token",
    "admin:public_key": "token has SSH key admin access — consider a narrower token",
}


def _collect_scope_warnings(scopes: list[str]) -> list[str]:
    """Raise InsufficientScopeError if required scopes missing; return overbroad warnings."""
    scope_set = set(scopes)
    missing = sorted(_REQUIRED_SCOPES - scope_set)
    if missing:
        raise InsufficientScopeError(missing_scopes=missing)
    warnings: list[str] = []
    for scope in scopes:
        if scope in _OVERBROAD_SCOPES:
            warnings.append(_OVERBROAD_SCOPES[scope])
        elif scope not in _KNOWN_SAFE_SCOPES:
            warnings.append(f"unrecognised scope '{scope}' — consider using a narrower token")
    return warnings


@dataclass(frozen=True)
class GitHubIntegrationStatusDTO:
    """Read-model DTO returned by get_status."""

    is_connected: bool
    auth_method: GitHubAuthMethod | None
    github_login: str | None
    github_user_id: int | None
    status: GitHubIntegrationStatus | None
    last_synced_at: datetime | None
    repo_count: int


class ManageGitHubIntegrationUseCase:
    """Validate, store, query, and revoke a user's GitHub integration token."""

    def __init__(
        self,
        repository: GitHubIntegrationRepositoryPort,
        gateway_factory: Callable[[str], GitHubGateway],
    ) -> None:
        self._repo = repository
        self._gateway_factory = gateway_factory

    async def validate_and_store(
        self,
        token: str,
        auth_method: GitHubAuthMethod,
        user_id: int,
        *,
        correlation_id: str,
    ) -> tuple[GitHubIntegrationRecord, list[str]]:
        """Validate token scopes, encrypt it, and upsert the integration row.

        Returns (integration_record, scope_warnings).

        Raises:
            InsufficientScopeError: token is missing required scopes.
            InvalidGitHubTokenError: GitHub rejected the token (401/403).
        """
        with use_case_span(
            "github_integration.validate_and_store", user_id=user_id, correlation_id=correlation_id
        ):
            async with self._gateway_factory(token) as gh:
                try:
                    gh_user, scopes = await gh.get_user_with_scopes()
                except GitHubAuthError as exc:
                    raise InvalidGitHubTokenError(f"Token rejected by GitHub: {exc}") from exc

                if not scopes:
                    # Fine-grained PAT: scope names are opaque; probe capability instead
                    if not await gh.probe_repository_access():
                        raise InsufficientScopeError(missing_scopes=["repository access"])
                    token_scopes_value = "fine-grained"
                    scope_warnings: list[str] = []
                else:
                    scope_warnings = _collect_scope_warnings(scopes)
                    token_scopes_value = ", ".join(scopes)

            encrypted = encrypt_token(token)

            record = await self._repo.upsert(
                GitHubIntegrationUpsert(
                    user_id=user_id,
                    auth_method=auth_method,
                    encrypted_token=encrypted,
                    token_scopes=token_scopes_value,
                    github_login=gh_user.login,
                    github_user_id=gh_user.id,
                    status=GitHubIntegrationStatus.ACTIVE,
                )
            )

            logger.info(
                "github_integration_connected",
                extra={
                    "correlation_id": correlation_id,
                    "user_id": user_id,
                    "auth_method": auth_method.value,
                    "github_login": gh_user.login,
                },
            )
            return record, scope_warnings

    async def get_status(self, user_id: int) -> GitHubIntegrationStatusDTO:
        """Return current integration status DTO. is_connected=False when no row exists."""
        with use_case_span("github_integration.get_status", user_id=user_id):
            record = await self._repo.get_by_user_id(user_id)
            if record is None:
                return GitHubIntegrationStatusDTO(
                    is_connected=False,
                    auth_method=None,
                    github_login=None,
                    github_user_id=None,
                    status=None,
                    last_synced_at=None,
                    repo_count=0,
                )

            repo_count = await self._repo.count_repositories(user_id)

            return GitHubIntegrationStatusDTO(
                is_connected=True,
                auth_method=record.auth_method,
                github_login=record.github_login,
                github_user_id=record.github_user_id,
                status=record.status,
                last_synced_at=record.last_synced_at,
                repo_count=repo_count,
            )

    async def revoke(self, user_id: int) -> None:
        """Delete the integration row (user revokes on github.com themselves)."""
        with use_case_span("github_integration.revoke", user_id=user_id):
            await self._repo.delete_by_user_id(user_id)
