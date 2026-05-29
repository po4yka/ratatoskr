"""SQLAlchemy adapter for GitHub integration persistence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.application.ports.github_integration import (
    GitHubAuthMethod,
    GitHubIntegrationRecord,
    GitHubIntegrationRepositoryPort,
    GitHubIntegrationStatus,
    GitHubIntegrationUpsert,
)
from app.db.models.repository import (
    GitHubAuthMethod as OrmGitHubAuthMethod,
    GitHubIntegrationStatus as OrmGitHubIntegrationStatus,
    Repository,
    UserGitHubIntegration,
)

if TYPE_CHECKING:
    from app.db.session import Database


def _to_record(row: UserGitHubIntegration) -> GitHubIntegrationRecord:
    """Map an ORM row to the domain record DTO."""
    return GitHubIntegrationRecord(
        id=row.id,
        user_id=row.user_id,
        auth_method=GitHubAuthMethod(row.auth_method.value),
        encrypted_token=row.encrypted_token,
        token_scopes=row.token_scopes,
        github_login=row.github_login,
        github_user_id=row.github_user_id,
        status=GitHubIntegrationStatus(row.status.value),
        last_synced_at=row.last_synced_at,
    )


class GitHubIntegrationRepository:
    """Implements ``GitHubIntegrationRepositoryPort`` via SQLAlchemy."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_by_user_id(self, user_id: int) -> GitHubIntegrationRecord | None:
        async with self._db.session() as session:
            row = await session.scalar(
                select(UserGitHubIntegration).where(UserGitHubIntegration.user_id == user_id)
            )
        return _to_record(row) if row is not None else None

    async def upsert(self, payload: GitHubIntegrationUpsert) -> GitHubIntegrationRecord:
        orm_auth_method = OrmGitHubAuthMethod(payload.auth_method.value)
        orm_status = OrmGitHubIntegrationStatus(payload.status.value)

        async with self._db.transaction() as session:
            existing = await session.scalar(
                select(UserGitHubIntegration).where(
                    UserGitHubIntegration.user_id == payload.user_id
                )
            )
            if existing is None:
                row = UserGitHubIntegration(
                    user_id=payload.user_id,
                    auth_method=orm_auth_method,
                    encrypted_token=payload.encrypted_token,
                    token_scopes=payload.token_scopes,
                    github_login=payload.github_login,
                    github_user_id=payload.github_user_id,
                    status=orm_status,
                )
                session.add(row)
            else:
                existing.auth_method = orm_auth_method
                existing.encrypted_token = payload.encrypted_token
                existing.token_scopes = payload.token_scopes
                existing.github_login = payload.github_login
                existing.github_user_id = payload.github_user_id
                existing.status = orm_status
                row = existing

            await session.flush()
            await session.refresh(row)

        return _to_record(row)

    async def delete_by_user_id(self, user_id: int) -> None:
        async with self._db.transaction() as session:
            row = await session.scalar(
                select(UserGitHubIntegration).where(UserGitHubIntegration.user_id == user_id)
            )
            if row is not None:
                await session.delete(row)

    async def count_repositories(self, user_id: int) -> int:
        async with self._db.session() as session:
            return (
                await session.scalar(
                    select(func.count())
                    .select_from(Repository)
                    .where(Repository.user_id == user_id)
                )
                or 0
            )


# Verify structural conformance at import time (type-checker only; no runtime cost).
def _assert_implements_port(repo: GitHubIntegrationRepository) -> GitHubIntegrationRepositoryPort:
    return repo  # type: ignore[return-value]
