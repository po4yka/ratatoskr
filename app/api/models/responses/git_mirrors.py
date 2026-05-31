"""API response models for git mirror endpoints."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003  # used at runtime by Pydantic schema generation

from pydantic import BaseModel, ConfigDict

from app.api.models.responses.common import (  # noqa: TC001  # used at runtime by Pydantic schema generation
    PaginationInfo,
)


class GitMirrorCompact(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    clone_url: str
    name: str | None
    status: str
    source: str
    last_mirrored_at: datetime | None
    size_kb: int | None
    repository_id: int | None


class GitMirrorDetail(GitMirrorCompact):
    mirror_path: str | None
    default_branch: str | None
    consecutive_failures: int
    last_error: str | None
    last_error_category: str | None
    backoff_until: datetime | None
    last_attempt_at: datetime | None
    created_at: datetime
    updated_at: datetime


class GitMirrorListResponse(BaseModel):
    mirrors: list[GitMirrorCompact]
    pagination: PaginationInfo


class RegisterMirrorResponse(BaseModel):
    id: int
    status: str
    clone_url: str


class GitMirrorSearchItem(BaseModel):
    """Single result from semantic git mirror README search."""

    mirror_id: int
    clone_url: str
    name: str | None
    status: str
    source: str
    last_mirrored_at: datetime | None
    size_kb: int | None
    repository_id: int | None
    distance: float


class GitMirrorSearchResponse(BaseModel):
    items: list[GitMirrorSearchItem]
    total: int
    limit: int
