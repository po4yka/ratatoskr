"""Semantic search service for non-GitHub git mirror README entities."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.core.logging_utils import get_logger
from app.db.models.git_backup import GitMirror

logger = get_logger(__name__)


@dataclass(frozen=True)
class GitMirrorSearchResult:
    mirror_id: int
    clone_url: str
    name: str | None
    status: str
    source: str
    last_mirrored_at: Any  # datetime | None
    size_kb: int | None
    repository_id: int | None
    distance: float  # 1 - cosine similarity (lower = more similar)


@dataclass(frozen=True)
class GitMirrorSearchResults:
    items: list[GitMirrorSearchResult]
    total: int
    limit: int


class GitMirrorSearchService:
    """Semantic search over non-GitHub git mirror README vectors stored in Qdrant.

    Mirrors the structure of RepositorySearchService: embed query, native Qdrant
    filter on entity_type/user_id/environment/user_scope, hydrate GitMirror rows
    from DB (defense-in-depth user_id check), order by Qdrant score.
    """

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
        limit: int = 20,
        min_similarity: float = 0.2,
        correlation_id: str | None = None,
    ) -> GitMirrorSearchResults:
        """Search git mirror READMEs semantically, hard-scoped to the calling user."""
        if not query or not query.strip():
            msg = "query must be a non-empty string"
            raise ValueError(msg)

        if self._qdrant_store is None or not self._qdrant_store.available:
            logger.debug(
                "git_mirror_search_qdrant_unavailable",
                extra={"user_id": user_id, "correlation_id": correlation_id},
            )
            return GitMirrorSearchResults(items=[], total=0, limit=limit)

        # 1. Generate query embedding.
        try:
            embedding = await self._embedding_service.generate_embedding(
                query.strip(),
                language=None,
                task_type="query",
            )
        except Exception:
            logger.exception(
                "git_mirror_search_embedding_failed",
                extra={"correlation_id": correlation_id, "user_id": user_id},
            )
            return GitMirrorSearchResults(items=[], total=0, limit=limit)

        query_vector: list[float] = (
            embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
        )

        # 2. Build native Qdrant filter.
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        must: list[Any] = [
            FieldCondition(key="entity_type", match=MatchValue(value="git_mirror")),
            FieldCondition(key="user_id", match=MatchValue(value=user_id)),
            FieldCondition(key="environment", match=MatchValue(value=self._environment)),
            FieldCondition(key="user_scope", match=MatchValue(value=self._user_scope)),
        ]
        qdrant_filter = Filter(must=must)

        # 3. Query Qdrant.
        top_k = limit + 50  # buffer for DB-side mismatches
        try:
            client = self._qdrant_store._client
            collection_name = self._qdrant_store._collection_name

            response = await asyncio.to_thread(
                client.query_points,
                collection_name,
                list(query_vector),
                query_filter=qdrant_filter,
                limit=top_k,
                score_threshold=min_similarity,
                with_payload=True,
            )
            hits = response.points
        except Exception:
            logger.exception(
                "git_mirror_search_qdrant_failed",
                extra={"correlation_id": correlation_id, "user_id": user_id},
            )
            return GitMirrorSearchResults(items=[], total=0, limit=limit)

        if not hits:
            return GitMirrorSearchResults(items=[], total=0, limit=limit)

        # 4. Extract mirror_ids preserving Qdrant rank order.
        qdrant_scores: dict[int, float] = {}
        mirror_ids_ordered: list[int] = []
        for point in hits:
            payload = dict(point.payload or {})
            mid = payload.get("mirror_id")
            if mid is None:
                continue
            try:
                mid_int = int(mid)
            except (TypeError, ValueError):
                continue
            if mid_int not in qdrant_scores:
                mirror_ids_ordered.append(mid_int)
                qdrant_scores[mid_int] = float(point.score)

        if not mirror_ids_ordered:
            return GitMirrorSearchResults(items=[], total=0, limit=limit)

        # 5. Hydrate from Postgres (defense-in-depth: filter by user_id here too).
        async with self._db.session() as session:
            stmt = select(GitMirror).where(
                GitMirror.id.in_(mirror_ids_ordered),
                GitMirror.user_id == user_id,
            )
            result = await session.execute(stmt)
            db_rows: list[GitMirror] = list(result.scalars().all())

        # 6. Re-order to match Qdrant ranking and apply limit.
        db_by_id: dict[int, GitMirror] = {row.id: row for row in db_rows}
        ordered_rows = [(mid, db_by_id[mid]) for mid in mirror_ids_ordered if mid in db_by_id]
        total = len(ordered_rows)
        paged = ordered_rows[:limit]

        # 7. Build results.
        items: list[GitMirrorSearchResult] = []
        for mid, row in paged:
            similarity = max(0.0, min(1.0, qdrant_scores[mid]))
            distance = 1.0 - similarity
            items.append(
                GitMirrorSearchResult(
                    mirror_id=row.id,
                    clone_url=row.clone_url,
                    name=row.name,
                    status=row.status.value if hasattr(row.status, "value") else str(row.status),
                    source=row.source.value if hasattr(row.source, "value") else str(row.source),
                    last_mirrored_at=row.last_mirrored_at,
                    size_kb=row.size_kb,
                    repository_id=row.repository_id,
                    distance=distance,
                )
            )

        return GitMirrorSearchResults(items=items, total=total, limit=limit)
