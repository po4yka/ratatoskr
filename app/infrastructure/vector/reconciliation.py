"""Vector index reconciliation between Postgres and Qdrant."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select

from app.core.time_utils import UTC
from app.db.models import Repository, RepositoryEmbedding, Summary, SummaryEmbedding
from app.observability.metrics import record_vector_index_lag


@dataclass(frozen=True, slots=True)
class VectorReconciliationReport:
    status: str
    expected_summaries: int
    expected_repositories: int
    indexed_points: int | None = None
    indexed_summaries: int | None = None
    indexed_repositories: int | None = None
    missing_summary_vectors: int = 0
    missing_repository_vectors: int = 0
    stale_summary_embeddings: int = 0
    pending_summary_embeddings: int = 0
    missing_summary_embeddings: int = 0
    missing_repository_embeddings: int = 0
    stale_embedding_model_count: int = 0
    lag_seconds: float = 0.0
    oldest_unindexed_summary_updated_at: dt.datetime | None = None
    latest_indexed_at: dt.datetime | None = None
    vector_store_available: bool = False
    details: dict[str, object] = field(default_factory=dict)

    def to_diagnostics(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "missing_embeddings": self.missing_summary_embeddings
            + self.missing_repository_embeddings,
            "stale_embeddings": self.stale_summary_embeddings,
            "pending_embeddings": self.pending_summary_embeddings,
            "oldest_unindexed_summary_updated_at": self.oldest_unindexed_summary_updated_at,
            "latest_indexed_at": self.latest_indexed_at,
            "expected_summaries": self.expected_summaries,
            "expected_repositories": self.expected_repositories,
            "indexed_points": self.indexed_points,
            "indexed_summaries": self.indexed_summaries,
            "indexed_repositories": self.indexed_repositories,
            "missing_summary_vectors": self.missing_summary_vectors,
            "missing_repository_vectors": self.missing_repository_vectors,
            "stale_embedding_model_count": self.stale_embedding_model_count,
            "lag_seconds": self.lag_seconds,
            "vector_store_available": self.vector_store_available,
            "details": self.details,
        }


class VectorIndexReconciler:
    """Compare expected DB entities and embedding rows with Qdrant state."""

    def __init__(
        self,
        *,
        database: Any,
        vector_store: Any | None,
        expected_summary_models: set[str],
        expected_repository_models: set[str],
        expected_model_version: str = "1.0",
        scan_limit: int = 10_000,
    ) -> None:
        self._database = database
        self._vector_store = vector_store
        self._expected_summary_models = expected_summary_models
        self._expected_repository_models = expected_repository_models
        self._expected_model_version = expected_model_version
        self._scan_limit = scan_limit

    async def inspect(self, *, now: dt.datetime | None = None) -> VectorReconciliationReport:
        now = now or dt.datetime.now(UTC)
        async with self._database.session() as session:
            summary_ids = set(
                await session.scalars(
                    select(Summary.id).where(Summary.is_deleted.is_(False)).limit(self._scan_limit)
                )
            )
            repository_ids = set(
                await session.scalars(
                    select(Repository.id)
                    .where(Repository.analysis_json.is_not(None))
                    .limit(self._scan_limit)
                )
            )
            missing_summary_embeddings = int(
                await session.scalar(
                    select(func.count(Summary.id))
                    .outerjoin(SummaryEmbedding, SummaryEmbedding.summary_id == Summary.id)
                    .where(Summary.is_deleted.is_(False), SummaryEmbedding.id.is_(None))
                )
                or 0
            )
            stale_summary_embeddings = int(
                await session.scalar(
                    select(func.count(Summary.id))
                    .join(SummaryEmbedding, SummaryEmbedding.summary_id == Summary.id)
                    .where(
                        Summary.is_deleted.is_(False),
                        or_(
                            SummaryEmbedding.last_indexed_at.is_(None),
                            SummaryEmbedding.last_indexed_at < Summary.updated_at,
                        ),
                    )
                )
                or 0
            )
            pending_summary_embeddings = int(
                await session.scalar(
                    select(func.count(SummaryEmbedding.id)).where(
                        SummaryEmbedding.index_status != "indexed"
                    )
                )
                or 0
            )
            oldest_unindexed = await session.scalar(
                select(func.min(Summary.updated_at))
                .outerjoin(SummaryEmbedding, SummaryEmbedding.summary_id == Summary.id)
                .where(
                    Summary.is_deleted.is_(False),
                    or_(
                        SummaryEmbedding.id.is_(None),
                        SummaryEmbedding.last_indexed_at.is_(None),
                        SummaryEmbedding.last_indexed_at < Summary.updated_at,
                    ),
                )
            )
            latest_indexed = await session.scalar(
                select(func.max(SummaryEmbedding.last_indexed_at))
            )
            missing_repository_embeddings = int(
                await session.scalar(
                    select(func.count(Repository.id))
                    .outerjoin(
                        RepositoryEmbedding,
                        RepositoryEmbedding.repository_id == Repository.id,
                    )
                    .where(
                        Repository.analysis_json.is_not(None),
                        RepositoryEmbedding.id.is_(None),
                    )
                )
                or 0
            )
            stale_model_count = await self._stale_model_count(session)

        vector_available = bool(
            self._vector_store is not None and getattr(self._vector_store, "available", False)
        )
        indexed_points: int | None = None
        indexed_summary_ids: set[int] | None = None
        indexed_repository_ids: set[int] | None = None
        if vector_available:
            indexed_points = await asyncio.to_thread(self._vector_store.count)
            indexed_summary_ids = await asyncio.to_thread(
                self._vector_store.get_indexed_summary_ids,
                limit=self._scan_limit,
            )
            get_repo_ids = getattr(self._vector_store, "get_indexed_repository_ids", None)
            indexed_repository_ids = (
                await asyncio.to_thread(get_repo_ids, limit=self._scan_limit)
                if callable(get_repo_ids)
                else set()
            )

        missing_summary_vectors = (
            len(summary_ids - indexed_summary_ids) if indexed_summary_ids is not None else 0
        )
        missing_repository_vectors = (
            len(repository_ids - indexed_repository_ids)
            if indexed_repository_ids is not None
            else 0
        )
        lag_seconds = (
            max(0.0, (now - oldest_unindexed).total_seconds())
            if isinstance(oldest_unindexed, dt.datetime)
            else 0.0
        )
        issue_count = (
            missing_summary_embeddings
            + missing_repository_embeddings
            + stale_summary_embeddings
            + pending_summary_embeddings
            + stale_model_count
            + missing_summary_vectors
            + missing_repository_vectors
        )
        if self._vector_store is None:
            status = "disabled"
        elif not vector_available:
            status = "unavailable"
        else:
            status = "healthy" if issue_count == 0 else "degraded"

        report = VectorReconciliationReport(
            status=status,
            expected_summaries=len(summary_ids),
            expected_repositories=len(repository_ids),
            indexed_points=indexed_points,
            indexed_summaries=len(indexed_summary_ids) if indexed_summary_ids is not None else None,
            indexed_repositories=len(indexed_repository_ids)
            if indexed_repository_ids is not None
            else None,
            missing_summary_vectors=missing_summary_vectors,
            missing_repository_vectors=missing_repository_vectors,
            stale_summary_embeddings=stale_summary_embeddings,
            pending_summary_embeddings=pending_summary_embeddings,
            missing_summary_embeddings=missing_summary_embeddings,
            missing_repository_embeddings=missing_repository_embeddings,
            stale_embedding_model_count=stale_model_count,
            lag_seconds=lag_seconds,
            oldest_unindexed_summary_updated_at=oldest_unindexed,
            latest_indexed_at=latest_indexed,
            vector_store_available=vector_available,
            details={"scan_limit": self._scan_limit},
        )
        record_vector_index_lag(report.to_diagnostics())
        return report

    async def _stale_model_count(self, session: Any) -> int:
        summary_stale = await session.scalar(
            select(func.count(SummaryEmbedding.id)).where(
                or_(
                    SummaryEmbedding.model_version != self._expected_model_version,
                    SummaryEmbedding.model_name.notin_(self._expected_summary_models),
                )
            )
        )
        repo_stale = await session.scalar(
            select(func.count(RepositoryEmbedding.id)).where(
                or_(
                    RepositoryEmbedding.model_version != self._expected_model_version,
                    RepositoryEmbedding.model_name.notin_(self._expected_repository_models),
                )
            )
        )
        return int(summary_stale or 0) + int(repo_stale or 0)
