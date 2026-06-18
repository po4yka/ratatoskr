"""SQLAlchemy repository adapter for GitHub repository API read models."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import asc, desc, func, nulls_last, select
from sqlalchemy import delete as sql_delete
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.sql import cast

from app.application.dto.repository import (
    RepositoryAnalysisDTO,
    RepositoryCompactDTO,
    RepositoryDetailDTO,
    RepositoryListResult,
    RepositoryPaginationInfo,
)
from app.db.models.repository import Repository, RepositoryEmbedding

if TYPE_CHECKING:
    from app.db.session import Database


class RepositoryReadRepositoryAdapter:
    """SQLAlchemy adapter for repository list/detail/delete workflows."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def list_repositories(
        self,
        *,
        user_id: int,
        is_starred: bool | None,
        language: str | None,
        topic: str | None,
        source: Literal["manual", "starred"] | None,
        pending_analysis: bool | None,
        sort: Any,
        limit: int,
        offset: int,
    ) -> RepositoryListResult:
        async with self._db.session() as session:
            conditions = [Repository.user_id == user_id]
            if is_starred is not None:
                conditions.append(Repository.is_starred == is_starred)
            if language is not None:
                conditions.append(Repository.primary_language == language)
            if topic is not None:
                conditions.append(Repository.topics_json.contains(cast([topic], JSONB)))
            if source is not None:
                conditions.append(Repository.source == source)
            if pending_analysis is not None:
                conditions.append(Repository.pending_analysis == pending_analysis)

            count_stmt = select(func.count()).select_from(Repository).where(*conditions)
            total = int(await session.scalar(count_stmt) or 0)

            rows_stmt = (
                select(Repository)
                .where(*conditions)
                .order_by(*self._sort_clause(sort))
                .limit(limit)
                .offset(offset)
            )
            result = await session.execute(rows_stmt)
            rows = list(result.scalars().all())

        return RepositoryListResult(
            repositories=[self._repo_to_compact(row) for row in rows],
            pagination=RepositoryPaginationInfo(
                total=total,
                limit=limit,
                offset=offset,
                has_more=(offset + len(rows)) < total,
            ),
        )

    async def get_owned_repository(
        self,
        *,
        repository_id: int,
        user_id: int,
    ) -> RepositoryDetailDTO | None:
        row = await self.load_owned_repository(repository_id=repository_id, user_id=user_id)
        if row is None:
            return None
        return self._repo_to_detail(row)

    async def delete_owned_repository(
        self,
        *,
        repository_id: int,
        user_id: int,
    ) -> None:
        async with self._db.transaction() as session:
            await session.execute(
                sql_delete(RepositoryEmbedding).where(
                    RepositoryEmbedding.repository_id == repository_id
                )
            )
            await session.execute(
                sql_delete(Repository).where(
                    Repository.id == repository_id,
                    Repository.user_id == user_id,
                )
            )

    async def load_owned_repository(
        self,
        *,
        repository_id: int,
        user_id: int,
    ) -> Repository | None:
        """Load a repository only when it belongs to the authenticated user."""
        async with self._db.session() as session:
            stmt = select(Repository).where(
                Repository.id == repository_id,
                Repository.user_id == user_id,
            )
            return (await session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def _sort_clause(sort: Any) -> list[Any]:
        sort_value = getattr(sort, "value", sort)
        if sort_value == "stars_desc":
            return [desc(Repository.stars), desc(Repository.pushed_at)]
        if sort_value == "pushed_desc":
            return [nulls_last(desc(Repository.pushed_at))]
        if sort_value == "created_desc":
            return [desc(Repository.created_at)]
        return [asc(Repository.full_name)]

    @staticmethod
    def _repo_to_compact(row: Repository) -> RepositoryCompactDTO:
        topics: list[str] = list(row.topics_json) if isinstance(row.topics_json, list) else []
        return RepositoryCompactDTO(
            id=row.id,
            github_id=row.github_id,
            full_name=row.full_name,
            owner=row.owner,
            name=row.name,
            description=row.description,
            primary_language=row.primary_language,
            topics=topics,
            stars=row.stars,
            forks=row.forks,
            is_starred=row.is_starred,
            is_archived=row.is_archived,
            pushed_at=row.pushed_at,
            last_synced_at=row.last_synced_at,
            pending_analysis=row.pending_analysis,
            has_analysis=row.analysis_json is not None,
            source=row.source.value if hasattr(row.source, "value") else str(row.source),
        )

    @staticmethod
    def _repo_to_detail(row: Repository) -> RepositoryDetailDTO:
        topics: list[str] = list(row.topics_json) if isinstance(row.topics_json, list) else []
        languages: dict[str, int] = (
            dict(row.languages_json) if isinstance(row.languages_json, dict) else {}
        )
        analysis: RepositoryAnalysisDTO | None = None
        if row.analysis_json is not None:
            try:
                a = row.analysis_json
                analysis = RepositoryAnalysisDTO(
                    purpose=a.get("purpose", ""),
                    tech_stack=a.get("tech_stack", []),
                    architecture_summary=a.get("architecture_summary", ""),
                    key_concepts=[
                        kc if isinstance(kc, dict) else kc.model_dump()
                        for kc in a.get("key_concepts", [])
                    ],
                    code_patterns=[
                        cp if isinstance(cp, dict) else cp.model_dump()
                        for cp in a.get("code_patterns", [])
                    ],
                    use_cases=a.get("use_cases", []),
                    target_audience=a.get("target_audience", ""),
                    maturity=a.get("maturity", ""),
                    key_dependencies=a.get("key_dependencies", []),
                    hallucination_risk=a.get("hallucination_risk", ""),
                    confidence=a.get("confidence", 0.0),
                )
            except Exception:
                analysis = None

        return RepositoryDetailDTO(
            id=row.id,
            github_id=row.github_id,
            full_name=row.full_name,
            owner=row.owner,
            name=row.name,
            description=row.description,
            primary_language=row.primary_language,
            topics=topics,
            stars=row.stars,
            forks=row.forks,
            is_starred=row.is_starred,
            is_archived=row.is_archived,
            pushed_at=row.pushed_at,
            last_synced_at=row.last_synced_at,
            pending_analysis=row.pending_analysis,
            has_analysis=row.analysis_json is not None,
            source=row.source.value if hasattr(row.source, "value") else str(row.source),
            homepage_url=row.homepage_url,
            license_spdx=row.license_spdx,
            is_fork=row.is_fork,
            is_template=row.is_template,
            languages=languages,
            readme_excerpt=row.readme_excerpt,
            analysis=analysis,
            analysis_model=row.analysis_model,
            analysis_at=row.analysis_at,
            content_hash=row.content_hash,
            created_at_github=row.created_at_github,
            watchers=row.watchers,
        )
