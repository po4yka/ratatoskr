"""Single Qdrant-backed implementation of :class:`RetrievalPort` (ADR-0016).

This adapter is the ONE place vector retrieval scope filtering lives. Every
entity type goes through :meth:`_build_filter`, which unconditionally adds the
``environment`` + ``user_scope`` conditions (and, for user-scoped entities, the
``user_id`` condition) -- so the IDOR / tenant guard (CLAUDE.md rule 12,
ADR-0005/0012) is structurally impossible for a caller to omit. It absorbs both
the public ``QdrantVectorStore.query`` filter path AND the
``_client``/``_collection_name`` private bypass the repository / git-mirror
services hand-rolled (now via ``QdrantVectorStore.query_filter``).

The five legacy services remain the live path until the parity net (golden
byte-stable endpoint/MCP tests + scope-invariant tests against a live
Postgres + Qdrant) is green; this adapter is the introduce-the-port step of the
ADR-0018 strangler-fig migration. Caller cutover + the port-only import-linter
contract land in the parity-gated follow-up.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.application.dto.vector_search import EntityType, RetrievalHit, RetrievalResult
from app.core.logging_utils import get_logger
from app.db.models.core import Request, Summary
from app.db.models.git_backup import GitMirror
from app.db.models.repository import Repository
from app.infrastructure.vector.point_ids import (
    git_mirror_point_id,
    repository_point_id,
    summary_point_id,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from app.application.dto.vector_search import RetrievalScope

logger = get_logger(__name__)

# Entity types that carry a per-entity ``user_id`` payload and therefore get a
# mandatory ``user_id`` filter condition (defense-in-depth IDOR). ``x_wiki`` is
# shared wiki content and is scoped by environment + user_scope only.
_USER_SCOPED: frozenset[EntityType] = frozenset(
    {EntityType.SUMMARY, EntityType.REPOSITORY, EntityType.GIT_MIRROR}
)

# Qdrant payload key that carries each entity's primary id.
_ENTITY_ID_KEY: dict[EntityType, str] = {
    EntityType.SUMMARY: "summary_id",
    EntityType.REPOSITORY: "repository_id",
    EntityType.GIT_MIRROR: "mirror_id",
    EntityType.X_WIKI: "path",
}


class QdrantRetrievalAdapter:
    """Unified ``RetrievalPort`` over one Qdrant collection + Postgres hydration."""

    def __init__(
        self,
        *,
        vector_store: Any,
        embedding_service: Any,
        db: Any,
        reranker: Any | None = None,
        query_expansion: Any | None = None,
    ) -> None:
        self._vector_store = vector_store
        self._embedding_service = embedding_service
        self._db = db
        self._reranker = reranker
        self._query_expansion = query_expansion

    # ------------------------------------------------------------------
    # Port surface
    # ------------------------------------------------------------------

    async def retrieve(
        self,
        *,
        entity_type: EntityType,
        scope: RetrievalScope,
        query: str | None = None,
        vector: Sequence[float] | None = None,
        top_k: int = 10,
        filters: Mapping[str, Any] | None = None,
        rerank: bool = False,
        expand_query: bool = False,
        correlation_id: str | None = None,
    ) -> RetrievalResult:
        if query is None and vector is None:
            msg = "retrieve requires either query or vector"
            raise ValueError(msg)
        self._require_user_scope(entity_type, scope)

        query_vector: list[float] = (
            list(vector)
            if vector is not None
            else await self._embed(query or "", filters, expand_query=expand_query)
        )
        qdrant_filter = self._build_filter(entity_type, scope, filters)
        score_threshold = self._score_threshold(filters)

        vqresult = await asyncio.to_thread(
            self._vector_store.query_filter,
            query_vector,
            qdrant_filter,
            top_k,
            score_threshold=score_threshold,
        )
        hits = self._to_hits(entity_type, vqresult.hits)
        if entity_type in (EntityType.REPOSITORY, EntityType.GIT_MIRROR):
            hits = await self._hydrate(entity_type, hits, scope)
        if rerank and self._reranker is not None and query:
            hits = await self._rerank(query, hits)
        return RetrievalResult(hits=hits, total=len(hits))

    async def find_similar(
        self,
        *,
        entity_type: EntityType,
        entity_id: str,
        scope: RetrievalScope,
        top_k: int = 10,
        correlation_id: str | None = None,
    ) -> RetrievalResult:
        self._require_user_scope(entity_type, scope)
        point_id = await self._seed_point_id(entity_type, entity_id, scope)
        if not point_id:
            return RetrievalResult(hits=[], total=0)

        qdrant_filter = self._build_filter(entity_type, scope, None, exclude_point_id=point_id)
        vqresult = await asyncio.to_thread(
            self._vector_store.find_similar_by_id,
            point_id,
            qdrant_filter,
            top_k,
            score_threshold=None,
        )
        hits = self._to_hits(entity_type, vqresult.hits)
        if entity_type in (EntityType.REPOSITORY, EntityType.GIT_MIRROR):
            hits = await self._hydrate(entity_type, hits, scope)
        return RetrievalResult(hits=hits, total=len(hits))

    # ------------------------------------------------------------------
    # Centralized scope filter -- the single IDOR-safe filter builder
    # ------------------------------------------------------------------

    def _build_filter(
        self,
        entity_type: EntityType,
        scope: RetrievalScope,
        filters: Mapping[str, Any] | None,
        *,
        exclude_point_id: str | None = None,
    ) -> Any:
        """Build the native Qdrant filter for ``entity_type``.

        ``environment`` and ``user_scope`` are ALWAYS added; user-scoped
        entities also get ``user_id``. This is the only filter-build site, so a
        caller cannot produce an unscoped query. Per-entity branches reproduce
        the exact conditions the legacy services used (summary: language + tags,
        no ``entity_type`` match so legacy summary points without the field are
        still found; repository: primary_language MatchAny + topics MinShould +
        is_starred + source; git_mirror / x_wiki: entity_type only).
        """
        from qdrant_client.models import (
            FieldCondition,
            Filter,
            HasIdCondition,
            MatchAny,
            MatchValue,
            MinShould,
        )

        params = filters or {}
        must: list[Any] = [
            FieldCondition(key="environment", match=MatchValue(value=scope.environment)),
            FieldCondition(key="user_scope", match=MatchValue(value=scope.user_scope)),
        ]
        if entity_type in _USER_SCOPED and scope.user_id is not None:
            must.append(FieldCondition(key="user_id", match=MatchValue(value=scope.user_id)))

        should: list[Any] | None = None
        min_should: Any | None = None

        if entity_type is EntityType.SUMMARY:
            language = params.get("language")
            if language:
                must.append(FieldCondition(key="language", match=MatchValue(value=language)))
            for tag in params.get("tags") or []:
                must.append(FieldCondition(key="tags", match=MatchAny(any=[tag])))
        else:
            must.append(
                FieldCondition(key="entity_type", match=MatchValue(value=entity_type.value))
            )
            if entity_type is EntityType.REPOSITORY:
                languages = params.get("languages")
                if languages:
                    must.append(
                        FieldCondition(key="primary_language", match=MatchAny(any=languages))
                    )
                topics = params.get("topics")
                if topics:
                    should = [
                        FieldCondition(key="topics", match=MatchValue(value=t)) for t in topics
                    ]
                    min_should = MinShould(conditions=should, min_count=1)
                is_starred = params.get("is_starred")
                if is_starred is not None:
                    must.append(
                        FieldCondition(key="is_starred", match=MatchValue(value=is_starred))
                    )
                source = params.get("source")
                if source is not None:
                    must.append(FieldCondition(key="source", match=MatchValue(value=source)))

        must_not: list[Any] | None = None
        if exclude_point_id is not None:
            must_not = [HasIdCondition(has_id=[exclude_point_id])]

        return Filter(must=must, should=should, min_should=min_should, must_not=must_not)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require_user_scope(entity_type: EntityType, scope: RetrievalScope) -> None:
        if entity_type in (EntityType.REPOSITORY, EntityType.GIT_MIRROR) and scope.user_id is None:
            msg = (
                f"retrieval of entity_type={entity_type.value} requires scope.user_id (IDOR guard)"
            )
            raise ValueError(msg)

    @staticmethod
    def _score_threshold(filters: Mapping[str, Any] | None) -> float | None:
        if filters is None:
            return None
        value = filters.get("min_similarity")
        return None if value is None else float(value)

    async def _embed(
        self, query: str, filters: Mapping[str, Any] | None, *, expand_query: bool
    ) -> list[float]:
        text = query.strip()
        if expand_query and self._query_expansion is not None:
            # Provisional: append expansion terms as plain text before embedding.
            # The exact expansion-for-embedding semantics are tuned at cutover.
            try:
                expanded = self._query_expansion.expand_query(text)
                terms = getattr(expanded, "expanded_terms", None)
                if terms:
                    text = " ".join([text, *terms])
            except Exception:  # expansion is best-effort, never fatal
                logger.debug("retrieval_query_expansion_failed")
        language = (filters or {}).get("language")
        embedding = await self._embedding_service.generate_embedding(
            text, language=language, task_type="query"
        )
        vector: list[float] = (
            embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
        )
        return vector

    def _to_hits(self, entity_type: EntityType, vqhits: list[Any]) -> list[RetrievalHit]:
        hits: list[RetrievalHit] = []
        for hit in vqhits:
            metadata = dict(hit.metadata)
            entity_id = self._entity_id(entity_type, metadata)
            if entity_id is None:
                # Skip points that don't belong to this entity type (e.g. a
                # non-summary point lacking summary_id) -- reproduces the
                # hydration-skip the legacy summary service relied on.
                continue
            score = max(0.0, min(1.0, 1.0 - float(hit.distance)))
            hits.append(
                RetrievalHit(
                    entity_type=entity_type,
                    entity_id=entity_id,
                    point_id=str(hit.id),
                    score=score,
                    distance=1.0 - score,
                    payload=metadata,
                )
            )
        return hits

    @staticmethod
    def _entity_id(entity_type: EntityType, metadata: Mapping[str, Any]) -> str | None:
        value = metadata.get(_ENTITY_ID_KEY[entity_type])
        return None if value is None else str(value)

    async def _hydrate(
        self, entity_type: EntityType, hits: list[RetrievalHit], scope: RetrievalScope
    ) -> list[RetrievalHit]:
        if not hits or scope.user_id is None:
            return hits
        ids = [int(hit.entity_id) for hit in hits]
        rows: dict[int, Any] = {}
        async with self._db.session() as session:
            if entity_type is EntityType.REPOSITORY:
                repo_result = await session.execute(
                    select(Repository).where(
                        Repository.id.in_(ids), Repository.user_id == scope.user_id
                    )
                )
                rows = {row.id: row for row in repo_result.scalars().all()}
            else:
                mirror_result = await session.execute(
                    select(GitMirror).where(
                        GitMirror.id.in_(ids), GitMirror.user_id == scope.user_id
                    )
                )
                rows = {row.id: row for row in mirror_result.scalars().all()}

        # Preserve Qdrant rank order; drop hits whose row is missing (the
        # defense-in-depth Postgres-side user_id re-filter rejected it).
        hydrated: list[RetrievalHit] = []
        for hit in hits:
            row = rows.get(int(hit.entity_id))
            if row is None:
                continue
            hydrated.append(replace(hit, hydrated=self._row_to_dict(row)))
        return hydrated

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        return {column.name: getattr(row, column.name) for column in row.__table__.columns}

    async def _seed_point_id(
        self, entity_type: EntityType, entity_id: str, scope: RetrievalScope
    ) -> str:
        if entity_type is EntityType.REPOSITORY:
            return repository_point_id(scope.environment, scope.user_scope, int(entity_id))
        if entity_type is EntityType.GIT_MIRROR:
            return git_mirror_point_id(scope.environment, scope.user_scope, int(entity_id))
        if entity_type is EntityType.SUMMARY:
            request_id = await self._resolve_summary_request_id(int(entity_id), scope)
            return "" if request_id is None else summary_point_id(request_id, int(entity_id))
        msg = f"find_similar is not supported for entity_type={entity_type.value}"
        raise ValueError(msg)

    async def _resolve_summary_request_id(
        self, summary_id: int, scope: RetrievalScope
    ) -> int | None:
        stmt = select(Summary.request_id).where(Summary.id == summary_id)
        if scope.user_id is not None:
            stmt = stmt.join(Request, Summary.request_id == Request.id).where(
                Request.user_id == scope.user_id
            )
        async with self._db.session() as session:
            request_id: int | None = await session.scalar(stmt)
            return request_id

    async def _rerank(self, query: str, hits: list[RetrievalHit]) -> list[RetrievalHit]:
        if not hits:
            return hits
        documents = [
            {
                "id": hit.point_id,
                "title": str(hit.payload.get("title") or ""),
                "text": str(hit.payload.get("text") or hit.payload.get("snippet") or ""),
            }
            for hit in hits
        ]
        ranked = await self._reranker.rerank(
            query, documents, text_field="text", title_field="title", id_field="id"
        )
        by_point_id = {hit.point_id: hit for hit in hits}
        ordered: list[RetrievalHit] = []
        seen: set[str] = set()
        for document in ranked:
            point_id = document.get("id")
            hit = by_point_id.get(point_id)
            if hit is not None and point_id not in seen:
                ordered.append(hit)
                seen.add(point_id)
        # Append any hits the reranker dropped, preserving original order.
        ordered.extend(hit for hit in hits if hit.point_id not in seen)
        return ordered
