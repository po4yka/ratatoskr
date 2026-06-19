# ruff: noqa: TC001
"""API response models for GitHub repository endpoints."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime by Pydantic schema generation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.api.models.responses.common import PaginationInfo


class RepositoryCompact(BaseModel):
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
    source: str  # "manual" | "starred"


class RepositoryAnalysis(BaseModel):
    """Mirror of RepoAnalysis schema for API serialization."""

    model_config = ConfigDict(from_attributes=True)

    purpose: str
    tech_stack: list[str]
    architecture_summary: str
    key_concepts: list[dict[str, Any]]  # [{term, explanation}]
    code_patterns: list[dict[str, Any]]  # [{name, description}]
    use_cases: list[str]
    target_audience: str
    maturity: str
    key_dependencies: list[str]
    hallucination_risk: str
    confidence: float


class RepositoryDetail(RepositoryCompact):
    homepage_url: str | None
    license_spdx: str | None
    is_fork: bool
    is_template: bool
    languages: dict[str, int] = Field(default_factory=dict)
    readme_excerpt: str | None
    analysis: RepositoryAnalysis | None
    analysis_model: str | None
    analysis_at: datetime | None
    content_hash: str | None
    created_at_github: datetime | None
    watchers: int


class RepositoryListResponse(BaseModel):
    repositories: list[RepositoryCompact]
    pagination: PaginationInfo


class RepositoryWatch(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    repository: RepositoryCompact
    watch_readme: bool
    watch_releases: bool
    last_readme_sha256: str | None
    last_notified_readme_sha256: str | None
    last_release_tag: str | None
    last_notified_release_tag: str | None
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RepositoryWatchListResponse(BaseModel):
    watches: list[RepositoryWatch]
    pagination: PaginationInfo


class IngestRepositoryResponse(BaseModel):
    repository_id: int
    status: Literal["pending", "ready"]
    full_name: str


class RepositorySearchHit(RepositoryCompact):
    distance: float


class RepositorySearchResponse(BaseModel):
    results: list[RepositorySearchHit]
    pagination: PaginationInfo
    query: str
