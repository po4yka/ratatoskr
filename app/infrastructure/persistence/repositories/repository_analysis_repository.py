"""SQLAlchemy adapter for repository-analysis persistence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.application.ports.repository_analysis import RepositoryAnalysisRecord
from app.db.models.repository import Repository

if TYPE_CHECKING:
    from app.db.session import Database


class RepositoryAnalysisRepositoryAdapter:
    """Repository-analysis persistence backed by SQLAlchemy."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_for_analysis(self, repository_id: int) -> RepositoryAnalysisRecord | None:
        """Return repository content signals and cached analysis fields."""
        async with self._db.session() as session:
            result = await session.execute(select(Repository).where(Repository.id == repository_id))
            row = result.scalar_one_or_none()

        return _to_record(row) if row is not None else None

    async def save_analysis(
        self,
        repository_id: int,
        *,
        analysis_json: dict[str, Any],
        content_hash: str,
    ) -> RepositoryAnalysisRecord | None:
        """Persist analysis results inside an explicit transaction."""
        async with self._db.transaction() as session:
            row = await session.get(Repository, repository_id)
            if row is None:
                return None

            row.analysis_json = analysis_json
            row.analysis_model = None
            row.analysis_at = datetime.now(UTC)
            row.content_hash = content_hash
            row.pending_analysis = False
            await session.flush()
            return _to_record(row)


def _to_record(row: Repository) -> RepositoryAnalysisRecord:
    source = row.source.value if hasattr(row.source, "value") else str(row.source)
    return RepositoryAnalysisRecord(
        id=row.id,
        user_id=row.user_id,
        github_id=row.github_id,
        full_name=row.full_name,
        description=row.description,
        topics_json=list(row.topics_json) if isinstance(row.topics_json, list) else row.topics_json,
        languages_json=dict(row.languages_json)
        if isinstance(row.languages_json, dict)
        else row.languages_json,
        analysis_json=dict(row.analysis_json) if isinstance(row.analysis_json, dict) else None,
        content_hash=row.content_hash,
        pending_analysis=row.pending_analysis,
        readme_excerpt=row.readme_excerpt,
        primary_language=row.primary_language,
        license_spdx=row.license_spdx,
        default_branch=row.default_branch,
        is_starred=row.is_starred,
        source=source,
        created_at=row.created_at,
    )
