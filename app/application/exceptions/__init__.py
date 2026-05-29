"""Application-layer exception hierarchy.

Canonical exception classes for the application layer live here.
Adapter and infrastructure layers may re-export these for backward compatibility.
"""

from __future__ import annotations

from .github import (
    GitHubAuthError,
    GitHubError,
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
    "GitHubIntegrationRequiredError",
    "GitHubNotFoundError",
    "GitHubRateLimitError",
    "GitHubServerError",
    "InsufficientScopeError",
    "InvalidGitHubTokenError",
]
