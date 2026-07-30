"""GitHub API exception hierarchy.

Canonical definitions live in ``app.application.exceptions.github``.
This module re-exports them so adapter code and existing ``except`` sites
continue to work without changes.
"""

from __future__ import annotations

from app.application.exceptions.github import (
    GitHubAuthError,
    GitHubError,
    GitHubForbiddenError,
    GitHubIntegrationRequiredError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubServerError,
    InsufficientScopeError,
    InvalidGitHubTokenError,
)

__all__ = [
    "GitHubAuthError",
    "GitHubError",
    "GitHubForbiddenError",
    "GitHubIntegrationRequiredError",
    "GitHubNotFoundError",
    "GitHubRateLimitError",
    "GitHubServerError",
    "InsufficientScopeError",
    "InvalidGitHubTokenError",
]
