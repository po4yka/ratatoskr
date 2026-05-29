"""Port: GitHub API gateway.

Defines the structural interface the application layer depends on for
interacting with the GitHub REST API. The concrete adapter
(``app.adapters.github.github_api_client.GitHubAPIClient``) satisfies this
Protocol structurally — no changes to the adapter are required.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GitHubUserPort(Protocol):
    """Minimal user object returned by the gateway."""

    @property
    def login(self) -> str:
        """GitHub username."""
        ...

    @property
    def id(self) -> int:
        """GitHub numeric user ID."""
        ...


@runtime_checkable
class GitHubGateway(Protocol):
    """Async context manager + operations the use case depends on."""

    async def __aenter__(self) -> GitHubGateway:
        """Enter the async context and return self."""
        ...

    async def __aexit__(self, *exc: Any) -> None:
        """Exit the async context and release resources."""
        ...

    async def get_user_with_scopes(self) -> tuple[GitHubUserPort, list[str]]:
        """Return (authenticated_user, scopes).

        An empty scopes list signals a fine-grained PAT.
        """
        ...

    async def probe_repository_access(self) -> bool:
        """Return True when the token has repository-read capability.

        Used for fine-grained PAT validation where scope names are opaque.
        """
        ...


# A callable that accepts a token string and returns a GitHubGateway.
# Injected by the DI/composition site so the application module never imports
# the concrete adapter.
GitHubGatewayFactory = Callable[[str], GitHubGateway]
