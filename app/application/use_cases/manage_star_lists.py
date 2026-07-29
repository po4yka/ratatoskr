"""Use case: create, rename, delete star lists and set a repository's membership.

Star lists are the user's own categorization of what they starred, and GitHub
exposes them only through GraphQL mutations. Everything here needs the ``user``
OAuth scope — a token that can *read* lists is not necessarily allowed to write
them. Rather than spending a round trip on a pre-flight scope check, the gateway
error is left to surface: GitHub's own message names the missing scope, which is
more actionable than anything reconstructed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.application.exceptions.github import GitHubError, GitHubIntegrationRequiredError
from app.application.ports.github_integration import GitHubIntegrationStatus
from app.application.use_cases._tracing import use_case_span
from app.core.logging_utils import get_logger
from app.security.token_crypto import decrypt_token

if TYPE_CHECKING:
    from app.application.ports.github_integration import GitHubIntegrationRepositoryPort
    from app.application.ports.repositories import RepositoryReadRepositoryPort
    from app.application.ports.star_lists import StarListGatewayFactory

logger = get_logger(__name__)


class StarListNotFoundError(GitHubError):
    """No star list matches the supplied slug or name."""


class RepositoryNotOwnedError(GitHubError):
    """The repository does not exist or does not belong to the caller."""


@dataclass(frozen=True)
class StarListDescriptor:
    """A star list as returned to callers."""

    id: str
    name: str
    slug: str
    description: str
    is_private: bool


def _describe(entry: object) -> StarListDescriptor:
    return StarListDescriptor(
        id=str(getattr(entry, "id", "")),
        name=str(getattr(entry, "name", "")),
        slug=str(getattr(entry, "slug", "")),
        description=str(getattr(entry, "description", "") or ""),
        is_private=bool(getattr(entry, "is_private", False)),
    )


class ManageStarListsUseCase:
    """Star-list writes, plus the local mirror update that follows a write."""

    def __init__(
        self,
        *,
        gateway_factory: StarListGatewayFactory,
        repository_repo: RepositoryReadRepositoryPort,
        integration_repo: GitHubIntegrationRepositoryPort,
    ) -> None:
        self._gateway_factory = gateway_factory
        self._repository_repo = repository_repo
        self._integration_repo = integration_repo

    async def _token_for(self, user_id: int) -> str:
        """Decrypt the caller's GitHub token, or refuse without an active integration."""
        record = await self._integration_repo.get_by_user_id(user_id)
        if record is None or record.status != GitHubIntegrationStatus.ACTIVE:
            raise GitHubIntegrationRequiredError("No active GitHub integration for this user")
        return decrypt_token(record.encrypted_token)

    async def list_lists(self, user_id: int) -> list[StarListDescriptor]:
        """Return the user's lists (metadata only — no repository items)."""
        token = await self._token_for(user_id)
        async with self._gateway_factory(token) as gateway:
            summaries = await gateway.fetch_star_list_summaries()
        return [_describe(entry) for entry in summaries]

    async def create_list(
        self,
        user_id: int,
        *,
        name: str,
        description: str = "",
        is_private: bool = False,
    ) -> StarListDescriptor:
        """Create a list.

        GitHub caps a user at 32 lists and rejects the 33rd itself; that error
        is passed through rather than duplicated as a local pre-check, so the
        cap cannot drift out of step with GitHub.
        """
        token = await self._token_for(user_id)
        with use_case_span("star_lists.create"):
            async with self._gateway_factory(token) as gateway:
                created = await gateway.create_star_list(
                    name,
                    description=description,
                    is_private=is_private,
                )
        logger.info("star_list_created", extra={"name": name})
        return _describe(created)

    async def update_list(
        self,
        user_id: int,
        *,
        slug: str,
        name: str | None = None,
        description: str | None = None,
        is_private: bool | None = None,
    ) -> StarListDescriptor:
        """Rename or re-describe the list identified by *slug*."""
        token = await self._token_for(user_id)
        with use_case_span("star_lists.update"):
            async with self._gateway_factory(token) as gateway:
                target = await self._resolve(gateway, slug)
                updated = await gateway.update_star_list(
                    target.id,
                    name=name,
                    description=description,
                    is_private=is_private,
                )
        logger.info("star_list_updated", extra={"slug": slug})
        return _describe(updated)

    async def delete_list(self, user_id: int, *, slug: str) -> None:
        """Delete the list identified by *slug*; its repositories stay starred."""
        token = await self._token_for(user_id)
        with use_case_span("star_lists.delete"):
            async with self._gateway_factory(token) as gateway:
                target = await self._resolve(gateway, slug)
                await gateway.delete_star_list(target.id)
        logger.info("star_list_deleted", extra={"slug": slug})

    async def set_repository_lists(
        self,
        *,
        repository_id: int,
        user_id: int,
        list_names: list[str],
    ) -> list[str]:
        """Replace the set of lists a repository belongs to; return the new names.

        This is a full overwrite, matching the underlying mutation: an empty
        *list_names* removes the repository from every list. Callers that mean
        "also add to X" must send the union themselves.

        Unknown names are rejected up front instead of being silently dropped —
        a typo would otherwise read as "remove from that list".
        """
        repository = await self._repository_repo.get_owned_repository(
            repository_id=repository_id,
            user_id=user_id,
        )
        if repository is None:
            raise RepositoryNotOwnedError(f"No repository {repository_id} for this user")

        token = await self._token_for(user_id)
        with use_case_span("star_lists.set_repository_lists"):
            async with self._gateway_factory(token) as gateway:
                summaries = [
                    _describe(entry) for entry in await gateway.fetch_star_list_summaries()
                ]
                by_name = {entry.name: entry for entry in summaries}
                by_slug = {entry.slug: entry for entry in summaries}

                list_ids: list[str] = []
                for wanted in list_names:
                    match = by_name.get(wanted) or by_slug.get(wanted)
                    if match is None:
                        raise StarListNotFoundError(f"No star list named {wanted!r}")
                    list_ids.append(match.id)

                applied = await gateway.set_repository_lists(
                    owner=repository.owner,
                    name=repository.name,
                    list_ids=list_ids,
                )

        # Keep the local mirror in step so a filtered read right after the write
        # does not show the pre-change membership.
        await self._repository_repo.set_repository_list_names(
            repository_id=repository_id,
            user_id=user_id,
            list_names=applied,
        )
        logger.info(
            "star_list_membership_set",
            extra={"repository_id": repository_id, "lists": len(applied)},
        )
        return applied

    async def _resolve(self, gateway: object, slug: str) -> StarListDescriptor:
        """Find a list by slug, falling back to an exact name match."""
        summaries = [
            _describe(entry)
            for entry in await gateway.fetch_star_list_summaries()  # type: ignore[attr-defined]
        ]
        for entry in summaries:
            if slug in (entry.slug, entry.name):
                return entry
        raise StarListNotFoundError(f"No star list {slug!r}")
