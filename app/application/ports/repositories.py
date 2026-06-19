"""Repository read/write ports used by application services."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from app.application.dto.repository import (
        RepositoryDetailDTO,
        RepositoryListResult,
        RepositoryWatchDTO,
        RepositoryWatchListResult,
    )


@runtime_checkable
class RepositoryReadRepositoryPort(Protocol):
    """Persistence port for GitHub repository API read/write workflows."""

    async def list_repositories(
        self,
        *,
        user_id: int,
        is_starred: bool | None,
        language: str | None,
        topic: str | None,
        source: Literal["manual", "starred"] | None,
        pending_analysis: bool | None,
        sort: Any,
        limit: int,
        offset: int,
    ) -> RepositoryListResult:
        """Return a filtered page of repositories owned by user_id."""
        ...

    async def get_owned_repository(
        self,
        *,
        repository_id: int,
        user_id: int,
    ) -> RepositoryDetailDTO | None:
        """Return repository detail only when the row is owned by user_id."""
        ...

    async def delete_owned_repository(
        self,
        *,
        repository_id: int,
        user_id: int,
    ) -> None:
        """Delete a repository under a self-scoped owner predicate."""
        ...

    async def upsert_repository_watch(
        self,
        *,
        repository_id: int,
        user_id: int,
        watch_readme: bool,
        watch_releases: bool,
    ) -> RepositoryWatchDTO:
        """Create or update a repository watch owned by user_id."""
        ...

    async def delete_repository_watch(
        self,
        *,
        repository_id: int,
        user_id: int,
    ) -> bool:
        """Delete a repository watch owned by user_id; return whether a row was removed."""
        ...

    async def list_repository_watches(
        self,
        *,
        user_id: int,
        limit: int,
        offset: int,
    ) -> RepositoryWatchListResult:
        """Return watched repositories owned by user_id."""
        ...
