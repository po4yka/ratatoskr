"""GitHub exception hierarchy — canonical location in the application layer.

Adapter and infrastructure code may re-export these classes for backward
compatibility, but the application layer is the authoritative definition.
"""

from __future__ import annotations


class GitHubError(Exception):
    """Base GitHub error."""


class GitHubAuthError(GitHubError):
    """401 Unauthorized: token revoked, expired, or insufficient scope."""


class GitHubNotFoundError(GitHubError):
    """404 Not Found: repo doesn't exist or token can't see it."""


class GitHubRateLimitError(GitHubError):
    """403 with X-RateLimit-Remaining: 0 — rate limit exceeded."""

    def __init__(self, reset_epoch: int, message: str = "GitHub rate limit exceeded") -> None:
        super().__init__(message)
        self.reset_epoch = reset_epoch


class GitHubServerError(GitHubError):
    """5xx after retries exhausted."""


class GitHubIntegrationRequiredError(GitHubError):
    """Raised when a GitHub operation needs an active integration but none exists."""


class InvalidGitHubTokenError(GitHubError):
    """The token failed validation against GitHub /user."""


class InsufficientScopeError(InvalidGitHubTokenError):
    """Token is missing required scopes."""

    def __init__(self, missing_scopes: list[str]) -> None:
        self.missing_scopes = missing_scopes
        scopes_str = ", ".join(missing_scopes)
        super().__init__(
            f"Token is missing required scopes: {scopes_str}. "
            "Ratatoskr requires read:user and repo."
        )
