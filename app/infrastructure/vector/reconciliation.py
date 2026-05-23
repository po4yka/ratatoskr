"""Vector index reconciliation between Postgres and Qdrant."""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import func, or_, select

from app.core.time_utils import UTC
from app.db.models import Repository, RepositoryEmbedding, Summary, SummaryEmbedding
from app.observability.metrics import record_vector_index_lag


@dataclass(frozen=True, slots=True)
class VectorIndexedEntityStats:
    """Reconciliation stats for one indexed entity type."""

    entity_type: str
    expected_ids: set[int]
    indexed_ids: set[int] | None
    missing_embeddings: int = 0
    stale_embeddings: int = 0
    pending_embeddings: int = 0
    stale_model_count: int = 0
    oldest_unindexed_at: dt.datetime | None = None
    latest_indexed_at: dt.datetime | None = None

    @property
    def expected_count(self) -> int:
        return len(self.expected_ids)

    @property
    def indexed_count(self) -> int | None:
        return len(self.indexed_ids) if self.indexed_ids is not None else None

    @property
    def missing_vectors(self) -> int:
        return len(self.expected_ids - self.indexed_ids) if self.indexed_ids is not None else 0

    @property
    def issue_count(self) -> int:
        return (
            self.missing_embeddings
            + self.stale_embeddings
            + self.pending_embeddings
            + self.stale_model_count
            + self.missing_vectors
        )


class VectorIndexedEntityAdapter(Protocol):
    """Adapter for one entity type stored in the shared vector index."""

    entity_type: str

    async def inspect(
        self,
        session: Any,
        *,
        vector_store: Any | None,
        vector_store_available: bool,
        scan_limit: int,
        expected_model_version: str,
    ) -> VectorIndexedEntityStats:
        """Return DB and vector-store reconciliation stats for this entity type."""


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
        expected_summary_models: set[str] | None = None,
        expected_repository_models: set[str] | None = None,
        expected_model_version: str = "1.0",
        scan_limit: int = 10_000,
        adapters: list[VectorIndexedEntityAdapter] | None = None,
    ) -> None:
        self._database = database
        self._vector_store = vector_store
        self._expected_model_version = expected_model_version
        self._scan_limit = scan_limit
        self._adapters = adapters or [
            SummaryVectorIndexedEntityAdapter(expected_summary_models or set()),
            RepositoryVectorIndexedEntityAdapter(expected_repository_models or set()),
        ]

    async def inspect(self, *, now: dt.datetime | None = None) -> VectorReconciliationReport:
        now = now or dt.datetime.now(UTC)
        vector_available = bool(
            self._vector_store is not None and getattr(self._vector_store, "available", False)
        )
        async with self._database.session() as session:
            stats = [
                await adapter.inspect(
                    session,
                    vector_store=self._vector_store,
                    vector_store_available=vector_available,
                    scan_limit=self._scan_limit,
                    expected_model_version=self._expected_model_version,
                )
                for adapter in self._adapters
            ]

        indexed_points: int | None = None
        if vector_available:
            indexed_points = await asyncio.to_thread(self._vector_store.count)
        summary_stats = _stats_for(stats, "summary")
        repository_stats = _stats_for(stats, "repository")
        oldest_unindexed = _oldest_unindexed(stats)
        latest_indexed = _latest_indexed(stats)
        lag_seconds = (
            max(0.0, (now - oldest_unindexed).total_seconds())
            if isinstance(oldest_unindexed, dt.datetime)
            else 0.0
        )
        issue_count = sum(item.issue_count for item in stats)
        if self._vector_store is None:
            status = "disabled"
        elif not vector_available:
            status = "unavailable"
        else:
            status = "healthy" if issue_count == 0 else "degraded"

        report = VectorReconciliationReport(
            status=status,
            expected_summaries=summary_stats.expected_count,
            expected_repositories=repository_stats.expected_count,
            indexed_points=indexed_points,
            indexed_summaries=summary_stats.indexed_count,
            indexed_repositories=repository_stats.indexed_count,
            missing_summary_vectors=summary_stats.missing_vectors,
            missing_repository_vectors=repository_stats.missing_vectors,
            stale_summary_embeddings=summary_stats.stale_embeddings,
            pending_summary_embeddings=summary_stats.pending_embeddings,
            missing_summary_embeddings=summary_stats.missing_embeddings,
            missing_repository_embeddings=repository_stats.missing_embeddings,
            stale_embedding_model_count=sum(item.stale_model_count for item in stats),
            lag_seconds=lag_seconds,
            oldest_unindexed_summary_updated_at=oldest_unindexed,
            latest_indexed_at=latest_indexed,
            vector_store_available=vector_available,
            details={
                "scan_limit": self._scan_limit,
                "entities": {
                    item.entity_type: {
                        "expected": item.expected_count,
                        "indexed": item.indexed_count,
                        "missing_vectors": item.missing_vectors,
                        "missing_embeddings": item.missing_embeddings,
                        "stale_embeddings": item.stale_embeddings,
                        "pending_embeddings": item.pending_embeddings,
                        "stale_model_count": item.stale_model_count,
                        "oldest_unindexed_at": item.oldest_unindexed_at,
                        "latest_indexed_at": item.latest_indexed_at,
                    }
                    for item in stats
                },
            },
        )
        record_vector_index_lag(report.to_diagnostics())
        return report


class SummaryVectorIndexedEntityAdapter:
    """Reconciliation adapter for summary vectors."""

    entity_type = "summary"

    def __init__(self, expected_models: set[str]) -> None:
        self._expected_models = expected_models

    async def inspect(
        self,
        session: Any,
        *,
        vector_store: Any | None,
        vector_store_available: bool,
        scan_limit: int,
        expected_model_version: str,
    ) -> VectorIndexedEntityStats:
        expected_ids = set(
            await session.scalars(
                select(Summary.id).where(Summary.is_deleted.is_(False)).limit(scan_limit)
            )
        )
        missing_embeddings = int(
            await session.scalar(
                select(func.count(Summary.id))
                .outerjoin(SummaryEmbedding, SummaryEmbedding.summary_id == Summary.id)
                .where(Summary.is_deleted.is_(False), SummaryEmbedding.id.is_(None))
            )
            or 0
        )
        stale_embeddings = int(
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
        pending_embeddings = int(
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
        latest_indexed = await session.scalar(select(func.max(SummaryEmbedding.last_indexed_at)))
        stale_model_count = int(
            await session.scalar(
                select(func.count(SummaryEmbedding.id)).where(
                    or_(
                        SummaryEmbedding.model_version != expected_model_version,
                        SummaryEmbedding.model_name.notin_(self._expected_models),
                    )
                )
            )
            or 0
        )
        indexed_ids: set[int] | None = None
        if vector_store_available and vector_store is not None:
            indexed_ids = await asyncio.to_thread(
                vector_store.get_indexed_summary_ids,
                limit=scan_limit,
            )
        return VectorIndexedEntityStats(
            entity_type=self.entity_type,
            expected_ids=expected_ids,
            indexed_ids=indexed_ids,
            missing_embeddings=missing_embeddings,
            stale_embeddings=stale_embeddings,
            pending_embeddings=pending_embeddings,
            stale_model_count=stale_model_count,
            oldest_unindexed_at=oldest_unindexed,
            latest_indexed_at=latest_indexed,
        )


class RepositoryVectorIndexedEntityAdapter:
    """Reconciliation adapter for repository vectors."""

    entity_type = "repository"

    def __init__(self, expected_models: set[str]) -> None:
        self._expected_models = expected_models

    async def inspect(
        self,
        session: Any,
        *,
        vector_store: Any | None,
        vector_store_available: bool,
        scan_limit: int,
        expected_model_version: str,
    ) -> VectorIndexedEntityStats:
        expected_ids = set(
            await session.scalars(
                select(Repository.id).where(Repository.analysis_json.is_not(None)).limit(scan_limit)
            )
        )
        missing_embeddings = int(
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
        stale_model_count = int(
            await session.scalar(
                select(func.count(RepositoryEmbedding.id)).where(
                    or_(
                        RepositoryEmbedding.model_version != expected_model_version,
                        RepositoryEmbedding.model_name.notin_(self._expected_models),
                    )
                )
            )
            or 0
        )
        indexed_ids: set[int] | None = None
        if vector_store_available and vector_store is not None:
            get_indexed_ids = getattr(vector_store, "get_indexed_repository_ids", None)
            indexed_ids = (
                await asyncio.to_thread(get_indexed_ids, limit=scan_limit)
                if callable(get_indexed_ids)
                else set()
            )
        return VectorIndexedEntityStats(
            entity_type=self.entity_type,
            expected_ids=expected_ids,
            indexed_ids=indexed_ids,
            missing_embeddings=missing_embeddings,
            stale_model_count=stale_model_count,
        )


def _stats_for(
    stats: list[VectorIndexedEntityStats],
    entity_type: str,
) -> VectorIndexedEntityStats:
    for item in stats:
        if item.entity_type == entity_type:
            return item
    return VectorIndexedEntityStats(entity_type=entity_type, expected_ids=set(), indexed_ids=None)


def _oldest_unindexed(stats: list[VectorIndexedEntityStats]) -> dt.datetime | None:
    values = [item.oldest_unindexed_at for item in stats if item.oldest_unindexed_at is not None]
    return min(values) if values else None


def _latest_indexed(stats: list[VectorIndexedEntityStats]) -> dt.datetime | None:
    values = [item.latest_indexed_at for item in stats if item.latest_indexed_at is not None]
    return max(values) if values else None
