"""Semantic search service for GitHub repository entities."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from sqlalchemy import select

from app.core.logging_utils import get_logger
from app.db.models.repository import Repository
from app.observability.metrics_repositories import REPOSITORY_SEARCH_LATENCY_SECONDS

if TYPE_CHECKING:
    from datetime import datetime

logger = get_logger(__name__)


@dataclass(frozen=True)
class RepositorySearchResult:
    repository_id: int
    github_id: int
    full_name: str
    owner: str
    name: str
    description: str | None
    primary_language: str | None
    topics: list[str]
    stars: int
    is_starred: bool
    pushed_at: datetime | None
    distance: float  # 1 - cosine similarity (lower = more similar)


@dataclass(frozen=True)
class RepositorySearchResults:
    items: list[RepositorySearchResult]
    total: int  # best-effort; set to len(items) if unknown
    limit: int
    offset: int


class RepositorySearchService:
    """Semantic search over GitHub repository entities stored in Qdrant."""

    def __init__(
        self,
        embedding_service: Any,
        qdrant_store: Any,
        db: Any,
        *,
        environment: str,
        user_scope: str,
    ) -> None:
        self._embedding_service = embedding_service
        self._qdrant_store = qdrant_store
        self._db = db
        self._environment = environment
        self._user_scope = user_scope

    async def search(
        self,
        query: str,
        *,
        user_id: int,
        languages: list[str] | None = None,
        topics: list[str] | None = None,
        is_starred: bool | None = None,
        source: Literal["manual", "starred"] | None = None,
        limit: int = 20,
        offset: int = 0,
        min_similarity: float = 0.2,
        correlation_id: str | None = None,
    ) -> RepositorySearchResults:
        """Search repositories semantically, hard-scoped to the calling user."""

        if not query or not query.strip():
            msg = "query must be a non-empty string"
            raise ValueError(msg)

        _timer = (
            REPOSITORY_SEARCH_LATENCY_SECONDS.time()
            if REPOSITORY_SEARCH_LATENCY_SECONDS is not None
            else None
        )
        if _timer is not None:
            _timer.__enter__()  # type: ignore[no-untyped-call, unused-ignore]
        try:
            return await self._search_body(
                query=query,
                user_id=user_id,
                languages=languages,
                topics=topics,
                is_starred=is_starred,
                source=source,
                limit=limit,
                offset=offset,
                min_similarity=min_similarity,
                correlation_id=correlation_id,
            )
        finally:
            if _timer is not None:
                _timer.__exit__(None, None, None)  # type: ignore[no-untyped-call, unused-ignore]

    async def _search_body(
        self,
        query: str,
        *,
        user_id: int,
        languages: list[str] | None = None,
        topics: list[str] | None = None,
        is_starred: bool | None = None,
        source: Literal["manual", "starred"] | None = None,
        limit: int = 20,
        offset: int = 0,
        min_similarity: float = 0.2,
        correlation_id: str | None = None,
    ) -> RepositorySearchResults:
        """Internal search implementation (called after validation and timing setup)."""
        # 1. Generate query embedding
        try:
            embedding = await self._embedding_service.generate_embedding(
                query.strip(),
                language=None,
                task_type="query",
            )
        except Exception:
            logger.exception(
                "repository_search_embedding_failed",
                extra={"correlation_id": correlation_id, "user_id": user_id},
            )
            return RepositorySearchResults(items=[], total=0, limit=limit, offset=offset)

        query_vector: list[float] = (
            embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
        )

        # 2. Build native Qdrant filter (bypasses QdrantQueryFilters which has
        #    extra="forbid" and doesn't know repo-specific fields).
        # Lazy import so the module loads even without qdrant_client installed.
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            MatchAny,
            MatchValue,
            MinShould,
        )

        must: list[Any] = [
            FieldCondition(key="entity_type", match=MatchValue(value="repository")),
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
            FieldCondition(key="environment", match=MatchValue(value=self._environment)),
            FieldCondition(key="user_scope", match=MatchValue(value=self._user_scope)),
        ]
        should: list[Any] = []

        if languages:
            must.append(FieldCondition(key="primary_language", match=MatchAny(any=languages)))

        if topics:
            should = [FieldCondition(key="topics", match=MatchValue(value=t)) for t in topics]

        if is_starred is not None:
            must.append(FieldCondition(key="is_starred", match=MatchValue(value=is_starred)))

        if source is not None:
            must.append(FieldCondition(key="source", match=MatchValue(value=source)))

        qdrant_filter = Filter(
            must=must,
            should=should if should else None,
            min_should=MinShould(conditions=should, min_count=1) if should else None,
        )

        # 3. Query Qdrant — +50 buffer for Postgres-side filter mismatches
        top_k = limit + offset + 50
        score_threshold = min_similarity  # Qdrant uses similarity (higher = better)

        try:
            client = self._qdrant_store._client
            collection_name = self._qdrant_store._collection_name

            response = await asyncio.to_thread(
                client.query_points,
                collection_name,
                list(query_vector),
                query_filter=qdrant_filter,
                limit=top_k,
                score_threshold=score_threshold,
                with_payload=True,
            )
            hits = response.points
        except Exception:
            logger.exception(
                "repository_search_qdrant_failed",
                extra={"correlation_id": correlation_id, "user_id": user_id},
            )
            return RepositorySearchResults(items=[], total=0, limit=limit, offset=offset)

        if not hits:
            return RepositorySearchResults(items=[], total=0, limit=limit, offset=offset)

        # 4. Extract repository_ids preserving Qdrant rank order
        qdrant_scores: dict[int, float] = {}
        repo_ids_ordered: list[int] = []
        for point in hits:
            payload = dict(point.payload or {})
            rid = payload.get("repository_id")
            if rid is None:
                continue
            try:
                rid_int = int(rid)
            except (TypeError, ValueError):
                continue
            if rid_int not in qdrant_scores:
                repo_ids_ordered.append(rid_int)
                qdrant_scores[rid_int] = float(point.score)

        if not repo_ids_ordered:
            return RepositorySearchResults(items=[], total=0, limit=limit, offset=offset)

        # 5. Hydrate from Postgres (defense-in-depth: filter by user_id here too)
        async with self._db.session() as session:
            stmt = select(Repository).where(
                Repository.id.in_(repo_ids_ordered),
                Repository.user_id == user_id,
            )
            result = await session.execute(stmt)
            db_rows: list[Repository] = list(result.scalars().all())

        # 6. Re-order to match Qdrant ranking
        db_by_id: dict[int, Repository] = {row.id: row for row in db_rows}
        ordered_rows = [(rid, db_by_id[rid]) for rid in repo_ids_ordered if rid in db_by_id]

        # 7. Apply offset and limit
        paged = ordered_rows[offset : offset + limit]

        # 8. Build results — distance = 1 - similarity_score
        items: list[RepositorySearchResult] = []
        for rid, row in paged:
            similarity = max(0.0, min(1.0, qdrant_scores[rid]))
            distance = 1.0 - similarity
            topics_list: list[str] = (
                list(row.topics_json) if isinstance(row.topics_json, list) else []
            )
            items.append(
                RepositorySearchResult(
                    repository_id=row.id,
                    github_id=row.github_id,
                    full_name=row.full_name,
                    owner=row.owner,
                    name=row.name,
                    description=row.description,
                    primary_language=row.primary_language,
                    topics=topics_list,
                    stars=row.stars,
                    is_starred=row.is_starred,
                    pushed_at=row.pushed_at,
                    distance=distance,
                )
            )

        return RepositorySearchResults(
            items=items,
            total=len(ordered_rows),
            limit=limit,
            offset=offset,
        )
