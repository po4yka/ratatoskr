"""GitHub URL detection and parsing utilities."""

from __future__ import annotations

import re
from urllib.parse import urlparse

GITHUB_REPO_URL_PATTERN = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,38}[a-zA-Z0-9])?)/"
    r"(?P<name>[a-zA-Z0-9._-]+?)(?:\.git)?/?$"
)

EXCLUDED_PATH_SEGMENTS = frozenset(
    {
        "issues",
        "pull",
        "pulls",
        "blob",
        "raw",
        "wiki",
        "actions",
        "releases",
        "tags",
        "branches",
        "network",
        "graphs",
        "settings",
        "discussions",
        "security",
        "projects",
        "tree",
        "commits",
        "commit",
        "compare",
        "pulse",
        "find",
        "labels",
        "milestones",
        "watchers",
        "stargazers",
        "forks",
        "contributors",
        "deployments",
    }
)


def is_github_repo_url(url: str) -> bool:
    """Return True if the URL is a GitHub repo root (not a sub-path)."""
    if not url:
        return False
    # Reject gist, raw, codeload, api subdomains
    if any(
        host in url
        for host in (
            "gist.github.com",
            "raw.githubusercontent",
            "codeload.github.com",
            "api.github.com",
        )
    ):
        return False
    match = GITHUB_REPO_URL_PATTERN.match(url.strip())
    if not match:
        return False
    # Reject if a known sub-path is present (the regex permits trailing /, but not extra path
    # segments). The regex above already rejects /owner/repo/anything; this check is
    # defense-in-depth via path parsing.
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) != 2:
        return False
    # removesuffix, not rstrip: rstrip strips any trailing run of ".git"
    # characters, so "pullit" -> "pull" and "findit" -> "find", both of which
    # are in EXCLUDED_PATH_SEGMENTS. Line 85 already got this right.
    if parts[1].removesuffix(".git") in EXCLUDED_PATH_SEGMENTS:
        return False
    return True


def parse_github_repo_url(url: str) -> tuple[str, str] | None:
    """Return (owner, name) tuple or None. Strips .git suffix and trailing slash."""
    if not is_github_repo_url(url):
        return None
    match = GITHUB_REPO_URL_PATTERN.match(url.strip())
    if not match:
        return None
    return match.group("owner"), match.group("name").removesuffix(".git")
