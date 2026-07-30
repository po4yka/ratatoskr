"""Use case: add a GitHub repository to the system in one of three modes.

``metadata``
    Fetch and index the repository. This is what a GitHub URL pasted into the bot
    has always produced, and it is the default so that path is unchanged.
``track``
    Metadata plus on-disk git backup, without touching the user's stars. For a
    repository worth keeping a copy of but not worth a public star.
``star``
    Metadata, a star on GitHub, automatic filing into one of the user's star
    lists, and git backup.

Ordering and failure policy
---------------------------
The steps run in increasing order of "how annoying is it to redo by hand":

1. metadata ingest — a failure means nothing happened; the caller gets an error.
2. the star — a failure still means nothing the user can see changed.
3. list filing and 4. backup enrollment — these run *after* something visible has
   already happened, so a failure is reported as a warning instead of raising.

Steps 3 and 4 are deliberately not rolled back. Removing a star because a list
write hit a missing OAuth scope would destroy the one thing that did succeed in
order to make the report tidier. The result says what was and was not done.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from app.application.exceptions.github import GitHubError
from app.application.use_cases._tracing import use_case_span
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.application.dto.repository import RepositoryDetailDTO
    from app.application.ports.add_repository import (
        MirrorEnrollmentPort,
        RepositoryIngestPort,
    )
    from app.application.ports.repositories import RepositoryReadRepositoryPort
    from app.application.use_cases.manage_star_lists import (
        ManageStarListsUseCase,
        StarListDescriptor,
    )
    from app.application.use_cases.suggest_star_lists import SuggestStarListsUseCase

logger = get_logger(__name__)

AddMode = Literal["metadata", "track", "star"]
ListSource = Literal["knn", "llm", "explicit", "none"]


class RepositoryIngestFailedError(GitHubError):
    """The repository could not be fetched or stored; nothing was changed."""


class StarManagementUnavailableError(GitHubError):
    """``mode=star`` was asked for on a deployment that cannot write stars."""


@dataclass(frozen=True)
class AddRepositoryResult:
    """What actually happened, including the parts that did not."""

    repository_id: int
    full_name: str
    mode: AddMode
    is_starred: bool
    lists_applied: list[str] = field(default_factory=list)
    list_suggestion_source: ListSource = "none"
    mirror_id: int | None = None
    warnings: list[str] = field(default_factory=list)


class AddRepositoryUseCase:
    """Ingest a repository, then optionally star, file and back it up."""

    def __init__(
        self,
        *,
        ingest: RepositoryIngestPort,
        repository_repo: RepositoryReadRepositoryPort,
        star_lists: ManageStarListsUseCase | None = None,
        suggester: SuggestStarListsUseCase | None = None,
        mirrors: MirrorEnrollmentPort | None = None,
    ) -> None:
        self._ingest = ingest
        self._repository_repo = repository_repo
        self._star_lists = star_lists
        self._suggester = suggester
        self._mirrors = mirrors

    async def add(
        self,
        *,
        url: str,
        user_id: int,
        mode: AddMode = "metadata",
        list_names: list[str] | None = None,
        correlation_id: str = "",
    ) -> AddRepositoryResult:
        """Add the repository at *url*; see the module docstring for the modes."""
        with use_case_span("repositories.add"):
            repository_id = await self._ingest.ingest(
                url=url,
                user_id=user_id,
                correlation_id=correlation_id,
            )
            repository = await self._repository_repo.get_owned_repository(
                repository_id=repository_id,
                user_id=user_id,
            )
            if repository is None:
                # The ingest just wrote this row, so its absence is a bug rather
                # than a permission problem — and every later step needs it.
                raise RepositoryIngestFailedError(
                    f"Repository {repository_id} is not readable after ingest"
                )

            if mode == "metadata":
                return AddRepositoryResult(
                    repository_id=repository_id,
                    full_name=repository.full_name,
                    mode=mode,
                    is_starred=repository.is_starred,
                )

            warnings: list[str] = []
            is_starred = repository.is_starred
            lists_applied: list[str] = []
            list_source: ListSource = "none"

            if mode == "star":
                if self._star_lists is None:
                    raise StarManagementUnavailableError(
                        "This deployment cannot write GitHub stars"
                    )
                await self._star_lists.star_repository(
                    user_id=user_id,
                    owner=repository.owner,
                    name=repository.name,
                )
                is_starred = True
                lists_applied, list_source = await self._file(
                    user_id=user_id,
                    repository=repository,
                    list_names=list_names,
                    warnings=warnings,
                    correlation_id=correlation_id,
                )

            mirror_id = await self._enroll(
                user_id=user_id,
                repository=repository,
                warnings=warnings,
            )

        logger.info(
            "repository_added",
            extra={
                "full_name": repository.full_name,
                "mode": mode,
                "lists": len(lists_applied),
                "mirror_id": mirror_id,
                "warnings": len(warnings),
            },
        )
        return AddRepositoryResult(
            repository_id=repository_id,
            full_name=repository.full_name,
            mode=mode,
            is_starred=is_starred,
            lists_applied=lists_applied,
            list_suggestion_source=list_source,
            mirror_id=mirror_id,
            warnings=warnings,
        )

    async def _file(
        self,
        *,
        user_id: int,
        repository: RepositoryDetailDTO,
        list_names: list[str] | None,
        warnings: list[str],
        correlation_id: str,
    ) -> tuple[list[str], ListSource]:
        """Put the repository in a star list. Never raises past this point."""
        assert self._star_lists is not None  # guarded by the caller

        try:
            live = await self._star_lists.list_lists(user_id)
        except Exception as exc:
            warnings.append(f"Could not read star lists: {exc}")
            return [], "none"

        wanted: list[str]
        source: ListSource
        if list_names is not None:
            wanted, source = list(list_names), "explicit"
        else:
            wanted, source = await self._suggest(
                user_id=user_id,
                repository=repository,
                live=live,
                correlation_id=correlation_id,
            )

        if not wanted:
            return [], source

        try:
            applied = await self._star_lists.set_repository_lists(
                repository_id=repository.id,
                user_id=user_id,
                list_names=wanted,
            )
        except Exception as exc:
            # The star already landed. Reporting the shortfall beats unstarring.
            warnings.append(f"Starred, but could not file it in a list: {exc}")
            return [], "none"

        return applied, source

    async def _suggest(
        self,
        *,
        user_id: int,
        repository: RepositoryDetailDTO,
        live: list[StarListDescriptor],
        correlation_id: str,
    ) -> tuple[list[str], ListSource]:
        if self._suggester is None or not live:
            return [], "none"

        from app.core.star_list_suggestion_schema import StarListCandidate

        candidates = [
            StarListCandidate(name=entry.name, description=entry.description or "")
            for entry in live
            if entry.name
        ]
        suggestion = await self._suggester.suggest(
            user_id=user_id,
            repository_id=repository.id,
            full_name=repository.full_name,
            description=repository.description,
            language=repository.primary_language,
            topics=list(repository.topics or []),
            readme_excerpt=repository.readme_excerpt,
            available_lists=candidates,
            correlation_id=correlation_id,
        )
        return list(suggestion.list_names), suggestion.source

    async def _enroll(
        self,
        *,
        user_id: int,
        repository: RepositoryDetailDTO,
        warnings: list[str],
    ) -> int | None:
        """Register the repository for git backup. Never raises."""
        if self._mirrors is None:
            warnings.append("Git backup is not configured; repository was not enrolled")
            return None
        try:
            return await self._mirrors.enroll(
                user_id=user_id,
                owner=repository.owner,
                name=repository.name,
                repository_id=repository.id,
                pinned=True,
            )
        except Exception as exc:
            warnings.append(f"Could not enroll the repository for backup: {exc}")
            return None
