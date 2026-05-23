"""Ports for repository analysis persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from datetime import datetime


@dataclass(slots=True)
class RepositoryAnalysisRecord:
    """Repository fields needed by the analysis workflow."""

    id: int
    user_id: int
    github_id: int
    full_name: str
    description: str | None
    topics_json: list[Any] | None
    languages_json: dict[str, Any] | None
    analysis_json: dict[str, Any] | None
    content_hash: str | None
    pending_analysis: bool
    readme_excerpt: str | None
    primary_language: str | None
    license_spdx: str | None
    default_branch: str | None
    is_starred: bool
    source: str
    created_at: datetime | None


@runtime_checkable
class RepositoryAnalysisRepositoryPort(Protocol):
    """Persistence operations used by the repository analysis use case."""

    async def get_for_analysis(self, repository_id: int) -> RepositoryAnalysisRecord | None:
        """Return repository content signals and cached analysis fields."""

    async def save_analysis(
        self,
        repository_id: int,
        *,
        analysis_json: dict[str, Any],
        content_hash: str,
    ) -> RepositoryAnalysisRecord | None:
        """Persist analysis results and return the updated repository snapshot."""
