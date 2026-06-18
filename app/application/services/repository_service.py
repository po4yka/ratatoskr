"""Application service for GitHub repository workflows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.application.dto.repository import RepositoryDetailDTO, RepositoryListResult
    from app.application.ports.repositories import RepositoryReadRepositoryPort
    from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase

logger = get_logger(__name__)


class RepositoryServiceNotFoundError(Exception):
    """Raised when the requested repository is absent or not owned by the user."""


class RepositoryService:
    """Owns repository read, refresh, and delete workflows for API adapters."""

    def __init__(
        self,
        *,
        repository_repo: RepositoryReadRepositoryPort,
        embedding_gen: Any | None = None,
    ) -> None:
        self._repository_repo = repository_repo
        self._embedding_gen = embedding_gen

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
        """List repositories for a user with API-supported filters and pagination."""
        return await self._repository_repo.list_repositories(
            user_id=user_id,
            is_starred=is_starred,
            language=language,
            topic=topic,
            source=source,
            pending_analysis=pending_analysis,
            sort=sort,
            limit=limit,
            offset=offset,
        )

    async def get_repository(self, *, repository_id: int, user_id: int) -> RepositoryDetailDTO:
        """Return full repository detail for an owned repository."""
        repository = await self._repository_repo.get_owned_repository(
            repository_id=repository_id,
            user_id=user_id,
        )
        if repository is None:
            raise RepositoryServiceNotFoundError("Repository not found")
        return repository

    async def reanalyze_repository(
        self,
        *,
        repository_id: int,
        user_id: int,
        use_case: AnalyzeRepositoryUseCase,
        correlation_id: str,
    ) -> RepositoryDetailDTO:
        """Force repository analysis, then reload the owned repository detail."""
        repository = await self._repository_repo.get_owned_repository(
            repository_id=repository_id,
            user_id=user_id,
        )
        if repository is None:
            raise RepositoryServiceNotFoundError("Repository not found")

        from app.application.use_cases.analyze_repository import RepositoryNotFoundError

        try:
            await use_case.analyze(
                repository_id,
                force=True,
                correlation_id=correlation_id,
            )
        except RepositoryNotFoundError as exc:
            raise RepositoryServiceNotFoundError("Repository not found") from exc

        updated_repository = await self._repository_repo.get_owned_repository(
            repository_id=repository_id,
            user_id=user_id,
        )
        if updated_repository is None:
            raise RepositoryServiceNotFoundError("Repository not found")
        return updated_repository

    async def delete_repository(self, *, repository_id: int, user_id: int) -> None:
        """Delete an owned repository and its embedding records."""
        repository = await self._repository_repo.get_owned_repository(
            repository_id=repository_id,
            user_id=user_id,
        )
        if repository is None:
            raise RepositoryServiceNotFoundError("Repository not found")

        await self._delete_repository_embedding_point(repository_id)
        await self._repository_repo.delete_owned_repository(
            repository_id=repository_id,
            user_id=user_id,
        )

    async def _delete_repository_embedding_point(self, repository_id: int) -> None:
        if self._embedding_gen is None:
            return
        delete_point = getattr(self._embedding_gen, "delete_repository_point", None)
        if not callable(delete_point):
            return
        try:
            await delete_point(repository_id)
        except Exception as exc:
            logger.warning(
                "delete_repository_qdrant_failed",
                extra={"repository_id": repository_id, "error": str(exc)},
            )
