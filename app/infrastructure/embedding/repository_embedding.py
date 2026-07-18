"""Embedding generator for GitHub repository entities."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.logging_utils import get_logger
from app.db.models.repository import Repository, RepositoryEmbedding
from app.infrastructure.vector.point_ids import repository_point_id

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.core.repo_analysis_schema import RepoAnalysis
    from app.db.session import Database
    from app.infrastructure.embedding.embedding_protocol import EmbeddingServiceProtocol
    from app.infrastructure.vector.qdrant_store import QdrantVectorStore

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RepositoryEmbeddingBatchItem:
    """Input row for a repository embedding batch regeneration."""

    repository: Repository
    analysis: RepoAnalysis | None
    correlation_id: str


@dataclass(frozen=True, slots=True)
class RepositoryEmbeddingBatchSuccess:
    """Successful row from a repository embedding batch regeneration."""

    repository_id: int
    embedding: RepositoryEmbedding


@dataclass(frozen=True, slots=True)
class RepositoryEmbeddingBatchFailure:
    """Failed row from a repository embedding batch regeneration."""

    repository_id: int
    full_name: str
    error: Exception


@dataclass(frozen=True, slots=True)
class RepositoryEmbeddingBatchResult:
    """Per-row result for a repository embedding batch regeneration."""

    successes: list[RepositoryEmbeddingBatchSuccess]
    failures: list[RepositoryEmbeddingBatchFailure]


@dataclass(frozen=True, slots=True)
class _PreparedRepositoryEmbedding:
    repository: Repository
    analysis: RepoAnalysis | None
    correlation_id: str
    text: str
    content_hash: str
    topics: list[str]


class RepositoryEmbeddingGenerator:
    """Generates and persists embeddings for GitHub repository entities.

    Idempotent: re-running with the same repository overwrites the existing
    RepositoryEmbedding row and Qdrant point.
    """

    def __init__(
        self,
        embedding_service: EmbeddingServiceProtocol,
        qdrant_store: QdrantVectorStore | None,
        db: Database,
        *,
        environment: str,
        user_scope: str,
    ) -> None:
        self._embedding_service = embedding_service
        self._qdrant = qdrant_store
        self._db = db
        self._environment = environment
        self._user_scope = user_scope

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def regenerate(
        self,
        repository: Repository,
        *,
        analysis: RepoAnalysis | None,
        correlation_id: str,
    ) -> RepositoryEmbedding:
        """Compose embedding text, generate vector, upsert DB row and Qdrant.

        Returns the persisted RepositoryEmbedding row.
        """
        prepared = self._prepare_embedding(
            RepositoryEmbeddingBatchItem(
                repository=repository,
                analysis=analysis,
                correlation_id=correlation_id,
            )
        )

        model_name = self._embedding_service.get_model_name(None)
        dimensions = await self._embedding_service.get_dimensions_async(None)

        embedding = await self._embedding_service.generate_embedding(
            prepared.text,
            language=None,
            task_type="document",
        )
        embedding_blob = self._embedding_service.serialize_embedding(embedding)

        model_version = "1.0"

        db_row = await self._upsert_db_row(
            repository_id=repository.id,
            model_name=model_name,
            model_version=model_version,
            embedding_blob=embedding_blob,
            dimensions=dimensions,
            language=None,
            content_hash=prepared.content_hash,
        )

        if await self._upsert_qdrant(
            repository=prepared.repository,
            topics=prepared.topics,
            embedding=embedding,
            correlation_id=correlation_id,
        ):
            await self._mark_db_rows_indexed([repository.id])

        logger.info(
            "repository_embedding_regenerated",
            extra={
                "event": "repository_embedding_regenerated",
                "correlation_id": correlation_id,
                "repository_id": repository.id,
                "full_name": repository.full_name,
                "model_name": model_name,
                "dimensions": dimensions,
            },
        )

        return db_row

    async def regenerate_batch(
        self,
        items: Sequence[RepositoryEmbeddingBatchItem],
    ) -> RepositoryEmbeddingBatchResult:
        """Regenerate repository embeddings using batched provider, DB, and Qdrant calls.

        If any batch step fails, the method falls back to the single-row path so
        callers can keep row-level success/error accounting.
        """
        if not items:
            return RepositoryEmbeddingBatchResult(successes=[], failures=[])

        prepared = [self._prepare_embedding(item) for item in items]
        model_name = self._embedding_service.get_model_name(None)
        dimensions = await self._embedding_service.get_dimensions_async(None)
        model_version = "1.0"

        try:
            embeddings = await self._embedding_service.generate_embeddings_batch(
                [item.text for item in prepared],
                language=None,
                task_type="document",
            )
            if len(embeddings) != len(prepared):
                msg = (
                    "Embedding batch provider returned "
                    f"{len(embeddings)} vectors for {len(prepared)} texts"
                )
                raise ValueError(msg)

            embedding_blobs = [
                self._embedding_service.serialize_embedding(embedding) for embedding in embeddings
            ]
            db_rows = await self._upsert_db_rows(
                prepared=prepared,
                model_name=model_name,
                model_version=model_version,
                embedding_blobs=embedding_blobs,
                dimensions=dimensions,
                language=None,
            )

            indexed_repository_ids = await self._upsert_qdrant_batch(
                prepared=prepared,
                embeddings=embeddings,
            )
            await self._mark_db_rows_indexed(indexed_repository_ids)
        except Exception:
            logger.exception(
                "repository_embedding_batch_regenerate_failed",
                extra={"count": len(items)},
            )
            return await self._regenerate_individually(items)

        successes = [
            RepositoryEmbeddingBatchSuccess(
                repository_id=embedding.repository_id,
                embedding=embedding,
            )
            for embedding in db_rows
        ]

        for item in prepared:
            logger.info(
                "repository_embedding_regenerated",
                extra={
                    "event": "repository_embedding_regenerated",
                    "correlation_id": item.correlation_id,
                    "repository_id": item.repository.id,
                    "full_name": item.repository.full_name,
                    "model_name": model_name,
                    "dimensions": dimensions,
                },
            )

        return RepositoryEmbeddingBatchResult(successes=successes, failures=[])

    async def delete_repository_point(self, repository_id: int) -> None:
        """Delete the Qdrant point for a repository embedding if Qdrant is available."""
        if self._qdrant is None or not self._qdrant.available:
            logger.debug(
                "repository_embedding_qdrant_delete_skipped",
                extra={"reason": "not_available", "repository_id": repository_id},
            )
            return

        from qdrant_client.models import PointIdsList

        point_id = repository_point_id(
            self._environment,
            self._user_scope,
            repository_id,
        )
        await asyncio.to_thread(
            self._qdrant._client.delete,
            collection_name=self._qdrant._collection_name,
            points_selector=PointIdsList(points=[point_id]),
            wait=True,
        )

    @staticmethod
    def compose_embedding_text(
        *,
        full_name: str,
        description: str | None,
        topics: list[str],
        primary_language: str | None,
        languages: list[str],
        analysis: RepoAnalysis | None,
        readme_excerpt: str | None,
        max_chars: int = 2000,
    ) -> str:
        """Compose weighted embedding text from repository metadata.

        Concatenation order (higher weight = earlier in string):
        1. full_name repeated twice
        2. description repeated twice
        3. analysis.purpose
        4. topics joined
        5. primary_language + languages joined
        6. analysis.tech_stack joined
        7. analysis.architecture_summary (truncated to 500 chars)
        8. readme_excerpt (truncated to remaining budget)

        Total capped at max_chars.
        """
        parts: list[str] = []

        # full_name x2
        parts.append(full_name)
        parts.append(full_name)

        # description x2
        if description:
            parts.append(description)
            parts.append(description)

        # analysis fields
        if analysis is not None:
            parts.append(analysis.purpose)
            if analysis.tech_stack:
                parts.append(" ".join(analysis.tech_stack))

        # topics
        if topics:
            parts.append(" ".join(topics))

        # languages
        lang_parts: list[str] = []
        if primary_language:
            lang_parts.append(primary_language)
        if languages:
            lang_parts.extend(lang for lang in languages if lang != primary_language)
        if lang_parts:
            parts.append(" ".join(lang_parts))

        # architecture summary (capped)
        if analysis is not None and analysis.architecture_summary:
            parts.append(analysis.architecture_summary[:500])

        # assemble all non-readme parts first
        text = " ".join(parts)
        if len(text) >= max_chars:
            return text[:max_chars]

        # append readme excerpt in remaining budget
        if readme_excerpt:
            remaining = max_chars - len(text) - 1  # -1 for separator space
            if remaining > 0:
                text = text + " " + readme_excerpt[:remaining]

        return text[:max_chars]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _prepare_embedding(
        self,
        item: RepositoryEmbeddingBatchItem,
    ) -> _PreparedRepositoryEmbedding:
        repository = item.repository
        languages: list[str] = (
            list(repository.languages_json.keys())
            if isinstance(repository.languages_json, dict)
            else []
        )
        topics: list[str] = (
            list(repository.topics_json) if isinstance(repository.topics_json, list) else []
        )

        text = self.compose_embedding_text(
            full_name=repository.full_name,
            description=repository.description,
            topics=topics,
            primary_language=repository.primary_language,
            languages=languages,
            analysis=item.analysis,
            readme_excerpt=repository.readme_excerpt,
        )
        return _PreparedRepositoryEmbedding(
            repository=repository,
            analysis=item.analysis,
            correlation_id=item.correlation_id,
            text=text,
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            topics=topics,
        )

    async def _regenerate_individually(
        self,
        items: Sequence[RepositoryEmbeddingBatchItem],
    ) -> RepositoryEmbeddingBatchResult:
        successes: list[RepositoryEmbeddingBatchSuccess] = []
        failures: list[RepositoryEmbeddingBatchFailure] = []

        for item in items:
            try:
                embedding = await self.regenerate(
                    item.repository,
                    analysis=item.analysis,
                    correlation_id=item.correlation_id,
                )
            except Exception as exc:
                failures.append(
                    RepositoryEmbeddingBatchFailure(
                        repository_id=item.repository.id,
                        full_name=item.repository.full_name,
                        error=exc,
                    )
                )
                continue

            successes.append(
                RepositoryEmbeddingBatchSuccess(
                    repository_id=item.repository.id,
                    embedding=embedding,
                )
            )

        return RepositoryEmbeddingBatchResult(successes=successes, failures=failures)

    async def _upsert_db_row(
        self,
        *,
        repository_id: int,
        model_name: str,
        model_version: str,
        embedding_blob: bytes,
        dimensions: int,
        language: str | None,
        content_hash: str | None = None,
    ) -> RepositoryEmbedding:
        """Upsert RepositoryEmbedding row keyed by repository_id."""
        async with self._db.transaction() as session:
            stmt = (
                pg_insert(RepositoryEmbedding)
                .values(
                    repository_id=repository_id,
                    model_name=model_name,
                    model_version=model_version,
                    embedding_blob=embedding_blob,
                    dimensions=dimensions,
                    language=language,
                    content_hash=content_hash,
                    index_status="pending",
                )
                .on_conflict_do_update(
                    index_elements=["repository_id"],
                    set_={
                        "model_name": model_name,
                        "model_version": model_version,
                        "embedding_blob": embedding_blob,
                        "dimensions": dimensions,
                        "language": language,
                        "content_hash": content_hash,
                        "index_status": "pending",
                    },
                )
                .returning(RepositoryEmbedding)
            )
            result = await session.execute(stmt)
            return result.scalar_one()

    async def _upsert_db_rows(
        self,
        *,
        prepared: Sequence[_PreparedRepositoryEmbedding],
        model_name: str,
        model_version: str,
        embedding_blobs: Sequence[bytes],
        dimensions: int,
        language: str | None,
    ) -> list[RepositoryEmbedding]:
        """Upsert RepositoryEmbedding rows keyed by repository_id in one transaction."""
        if len(prepared) != len(embedding_blobs):
            msg = "prepared rows and embedding blobs must have the same length"
            raise ValueError(msg)

        values = [
            {
                "repository_id": item.repository.id,
                "model_name": model_name,
                "model_version": model_version,
                "embedding_blob": embedding_blob,
                "dimensions": dimensions,
                "language": language,
                "content_hash": item.content_hash,
                "index_status": "pending",
            }
            for item, embedding_blob in zip(prepared, embedding_blobs, strict=True)
        ]

        insert_stmt = pg_insert(RepositoryEmbedding).values(values)
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["repository_id"],
            set_={
                "model_name": insert_stmt.excluded.model_name,
                "model_version": insert_stmt.excluded.model_version,
                "embedding_blob": insert_stmt.excluded.embedding_blob,
                "dimensions": insert_stmt.excluded.dimensions,
                "language": insert_stmt.excluded.language,
                "content_hash": insert_stmt.excluded.content_hash,
                "index_status": "pending",
            },
        ).returning(RepositoryEmbedding)

        async with self._db.transaction() as session:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())

        rows_by_repository_id = {row.repository_id: row for row in rows}
        return [rows_by_repository_id[item.repository.id] for item in prepared]

    async def _upsert_qdrant(
        self,
        *,
        repository: Repository,
        topics: list[str],
        embedding: Any,
        correlation_id: str,
    ) -> bool:
        if self._qdrant is None or not self._qdrant.available:
            logger.debug(
                "repository_embedding_qdrant_skipped",
                extra={
                    "reason": "not_available",
                    "repository_id": repository.id,
                    "correlation_id": correlation_id,
                },
            )
            return False

        point_id = repository_point_id(
            self._environment,
            self._user_scope,
            repository.id,
        )

        metadata = self._build_qdrant_metadata(repository=repository, topics=topics)

        vector: list[float] = (
            embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
        )

        acknowledged = await asyncio.to_thread(
            self._qdrant.upsert_notes,
            [vector],
            [metadata],
            [point_id],
        )
        return acknowledged is True

    async def _upsert_qdrant_batch(
        self,
        *,
        prepared: Sequence[_PreparedRepositoryEmbedding],
        embeddings: Sequence[Any],
    ) -> list[int]:
        if self._qdrant is None or not self._qdrant.available:
            logger.debug(
                "repository_embedding_qdrant_skipped",
                extra={"reason": "not_available", "count": len(prepared)},
            )
            return []

        vectors: list[list[float]] = []
        metadatas: list[dict[str, Any]] = []
        point_ids: list[str] = []

        for item, embedding in zip(prepared, embeddings, strict=True):
            repository = item.repository
            vectors.append(embedding.tolist() if hasattr(embedding, "tolist") else list(embedding))
            metadatas.append(
                self._build_qdrant_metadata(
                    repository=repository,
                    topics=item.topics,
                )
            )
            point_ids.append(
                repository_point_id(
                    self._environment,
                    self._user_scope,
                    repository.id,
                )
            )

        acknowledged = await asyncio.to_thread(
            self._qdrant.upsert_notes,
            vectors,
            metadatas,
            point_ids,
        )
        if acknowledged is not True:
            return []
        return [item.repository.id for item in prepared]

    async def _mark_db_rows_indexed(self, repository_ids: Sequence[int]) -> None:
        if not repository_ids:
            return
        from sqlalchemy import update

        from app.db.types import _utcnow

        async with self._db.transaction() as session:
            await session.execute(
                update(RepositoryEmbedding)
                .where(RepositoryEmbedding.repository_id.in_(list(repository_ids)))
                .values(last_indexed_at=_utcnow(), index_status="indexed")
            )

    def _build_qdrant_metadata(
        self,
        *,
        repository: Repository,
        topics: list[str],
    ) -> dict[str, Any]:
        created_at_iso = (
            repository.created_at.isoformat() if repository.created_at is not None else None
        )

        return {
            "entity_type": "repository",
            "repository_id": repository.id,
            "user_id": repository.user_id,
            "github_id": repository.github_id,
            "full_name": repository.full_name,
            "primary_language": repository.primary_language,
            "topics": topics,
            "is_starred": repository.is_starred,
            "source": repository.source.value
            if hasattr(repository.source, "value")
            else repository.source,
            "created_at": created_at_iso,
            "environment": self._environment,
            "user_scope": self._user_scope,
            "language": "en",
        }
