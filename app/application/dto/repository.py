"""Repository read-model DTOs for application services."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # Pydantic resolves this at runtime.
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RepositoryPaginationInfo(BaseModel):
    """Pagination metadata for repository list results."""

    total: int
    limit: int
    offset: int
    has_more: bool = Field(serialization_alias="hasMore")


class RepositoryCompactDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    github_id: int
    full_name: str
    owner: str
    name: str
    description: str | None
    primary_language: str | None
    topics: list[str] = Field(default_factory=list)
    stars: int
    forks: int
    is_starred: bool
    is_archived: bool
    pushed_at: datetime | None
    last_synced_at: datetime
    pending_analysis: bool
    has_analysis: bool
    source: str


class RepositoryAnalysisDTO(BaseModel):
    """Repository analysis projection used by repository read models."""

    model_config = ConfigDict(from_attributes=True)

    purpose: str
    tech_stack: list[str]
    architecture_summary: str
    key_concepts: list[dict[str, Any]]
    code_patterns: list[dict[str, Any]]
    use_cases: list[str]
    target_audience: str
    maturity: str
    key_dependencies: list[str]
    hallucination_risk: str
    confidence: float


class RepositoryDetailDTO(RepositoryCompactDTO):
    homepage_url: str | None
    license_spdx: str | None
    is_fork: bool
    is_template: bool
    languages: dict[str, int] = Field(default_factory=dict)
    readme_excerpt: str | None
    analysis: RepositoryAnalysisDTO | None
    analysis_model: str | None
    analysis_at: datetime | None
    content_hash: str | None
    created_at_github: datetime | None
    watchers: int


class RepositoryListResult(BaseModel):
    repositories: list[RepositoryCompactDTO]
    pagination: RepositoryPaginationInfo


class RepositoryWatchDTO(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    repository: RepositoryCompactDTO
    watch_readme: bool
    watch_releases: bool
    last_readme_sha256: str | None
    last_notified_readme_sha256: str | None
    last_release_tag: str | None
    last_notified_release_tag: str | None
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RepositoryWatchListResult(BaseModel):
    watches: list[RepositoryWatchDTO]
    pagination: RepositoryPaginationInfo
