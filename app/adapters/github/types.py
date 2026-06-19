"""GitHub API data transfer objects."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "AuthenticatedUserDTO",
    "GistDTO",
    "GitHubLicenseDTO",
    "GitHubOwnerDTO",
    "LanguagesDTO",
    "ReleaseDTO",
    "RepositoryDTO",
    "StarredItem",
]


class GitHubOwnerDTO(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    login: str
    id: int
    type: str  # "User" or "Organization"


class GitHubLicenseDTO(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    spdx_id: str | None = None
    name: str | None = None
    key: str | None = None


class RepositoryDTO(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    name: str
    full_name: str
    owner: GitHubOwnerDTO
    description: str | None = None
    homepage: str | None = None
    language: str | None = None
    topics: list[str] = Field(default_factory=list)
    stargazers_count: int = 0
    forks_count: int = 0
    watchers_count: int = 0
    default_branch: str | None = None
    license: GitHubLicenseDTO | None = None
    archived: bool = False
    fork: bool = False
    is_template: bool = False
    pushed_at: datetime | None = None
    created_at: datetime | None = None
    html_url: str
    # GitHub reports repository disk size in KB.  Present on all repo endpoints
    # (/repos/{owner}/{name}, /user/repos, /user/starred, /user/subscriptions).
    size: int = 0


class StarredItem(BaseModel):
    """Wrapper returned by /user/starred when Accept: application/vnd.github.star+json."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    starred_at: datetime
    repo: RepositoryDTO


class LanguagesDTO(BaseModel):
    """Map of language -> bytes-of-code, returned by /repos/{owner}/{name}/languages."""

    model_config = ConfigDict(extra="allow", frozen=True)

    def as_dict(self) -> dict[str, int]:
        return self.model_dump()


class GistDTO(BaseModel):
    """One gist returned by GET /gists."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: str
    git_pull_url: str
    description: str | None = None
    html_url: str
    updated_at: datetime


class ReleaseDTO(BaseModel):
    """Latest release returned by GET /repos/{owner}/{name}/releases/latest."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    tag_name: str
    name: str | None = None
    html_url: str
    published_at: datetime | None = None


class AuthenticatedUserDTO(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    id: int
    login: str
    name: str | None = None
    email: str | None = None
    type: str = "User"
