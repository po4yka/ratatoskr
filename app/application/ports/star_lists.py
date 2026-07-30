"""Port: GitHub star-list gateway.

Star lists exist only in GitHub's GraphQL schema, so this is a separate port
from :mod:`app.application.ports.github_gateway` (REST). The concrete adapter
(``app.adapters.github.github_graphql_client.GitHubGraphQLClient``) satisfies
the Protocol structurally; the application layer never imports it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StarListPort(Protocol):
    """One star list as the application layer sees it."""

    @property
    def id(self) -> str:
        """GraphQL node ID; required by every mutation."""
        ...

    @property
    def name(self) -> str:
        """Display name shown on the stars page."""
        ...

    @property
    def slug(self) -> str:
        """URL slug GitHub derives from the name."""
        ...


@runtime_checkable
class StarListGateway(Protocol):
    """Async context manager over the star-list read and write operations."""

    async def __aenter__(self) -> StarListGateway:
        """Enter the async context and return self."""
        ...

    async def __aexit__(self, *exc: Any) -> None:
        """Exit the async context and release resources."""
        ...

    async def fetch_star_lists(self) -> list[Any]:
        """Return every star list of the authenticated user with its repo IDs."""
        ...

    async def fetch_star_list_summaries(self) -> list[Any]:
        """Return the lists without their items — one request, no item paging."""
        ...

    async def create_star_list(
        self,
        name: str,
        *,
        description: str = "",
        is_private: bool = False,
    ) -> Any:
        """Create a list and return it."""
        ...

    async def update_star_list(
        self,
        list_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        is_private: bool | None = None,
    ) -> Any:
        """Rename or re-describe a list; omitted fields are left untouched."""
        ...

    async def delete_star_list(self, list_id: str) -> None:
        """Delete a list; the repositories in it keep their stars."""
        ...

    async def set_repository_lists(
        self,
        *,
        owner: str,
        name: str,
        list_ids: list[str],
    ) -> list[str]:
        """Replace the lists a repository belongs to; return the resulting names."""
        ...

    async def add_star(self, *, owner: str, name: str) -> None:
        """Star a repository. Idempotent — an already-starred repo is accepted."""
        ...

    async def remove_star(self, *, owner: str, name: str) -> None:
        """Unstar a repository. Idempotent, like :meth:`add_star`."""
        ...


# Injected by the composition site so the application module never imports the
# concrete adapter.
StarListGatewayFactory = Callable[[str], StarListGateway]
