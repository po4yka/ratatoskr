"""CLI tool to sync embeddings into the Qdrant vector store."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from sqlalchemy import select

from app.application.services.summary_embedding_generator import SummaryEmbeddingGenerator
from app.config import DatabaseConfig, QdrantConfig, load_config
from app.core.embedding_space import resolve_embedding_space_identifier
from app.core.logging_utils import get_logger
from app.db.models import Request, Summary, model_to_dict
from app.db.session import Database
from app.infrastructure.embedding.embedding_factory import create_embedding_service
from app.infrastructure.persistence.repositories.embedding_repository import (
    EmbeddingRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.request_repository import (
    RequestRepositoryAdapter,
)
from app.infrastructure.persistence.repositories.summary_repository import (
    SummaryRepositoryAdapter,
)
from app.infrastructure.vector.metadata_builder import MetadataBuilder
from app.infrastructure.vector.qdrant_store import QdrantVectorStore

logger = get_logger(__name__)


async def _fetch_summaries(db: Database, limit: int | None) -> list[dict[str, Any]]:
    async with db.session() as session:
        query = (
            select(Summary, Request)
            .join(Request, Summary.request_id == Request.id)
            .order_by(Summary.created_at.desc())
        )
        if limit:
            query = query.limit(limit)

        results = []
        rows = await session.execute(query)
        for summary, request in rows:
            item = model_to_dict(summary)
            if item:
                item["request_id"] = request.id
                item["request"] = model_to_dict(request)
                results.append(item)
        return results


def _summary_ids(summaries: list[dict[str, Any]]) -> list[int]:
    ids: list[int] = []
    for summary in summaries:
        summary_id = summary.get("id")
        if isinstance(summary_id, int):
            ids.append(summary_id)
    return ids


async def _fetch_embeddings_by_summary_id(
    embedding_repo: EmbeddingRepositoryAdapter,
    summary_ids: list[int],
) -> dict[int, dict[str, Any]]:
    embeddings = await embedding_repo.async_get_summary_embeddings(summary_ids)
    by_summary_id: dict[int, dict[str, Any]] = {}
    for embedding in embeddings:
        summary_id = embedding.get("summary_id")
        if isinstance(summary_id, int):
            by_summary_id[summary_id] = embedding
    return by_summary_id


def _as_vector(embedding: Any) -> list[float]:
    return embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)


async def _generate_chunk_window_vectors(
    embedding_service: Any,
    chunk_windows: list[tuple[str, dict[str, Any]]],
) -> tuple[list[list[float]], list[dict[str, Any]]]:
    vectors: list[list[float] | None] = [None] * len(chunk_windows)
    batches_by_language: dict[str | None, list[tuple[int, str]]] = {}

    for index, (text, metadata) in enumerate(chunk_windows):
        raw_language = metadata.get("language")
        language = raw_language if isinstance(raw_language, str) else None
        batches_by_language.setdefault(language, []).append((index, text))

    for language, batch in batches_by_language.items():
        embeddings = await embedding_service.generate_embeddings_batch(
            [text for _, text in batch],
            language=language,
            task_type="document",
        )
        if len(embeddings) != len(batch):
            msg = (
                f"Embedding batch returned {len(embeddings)} vectors for {len(batch)} chunk windows"
            )
            raise RuntimeError(msg)
        for (index, _text), embedding in zip(batch, embeddings, strict=True):
            vectors[index] = _as_vector(embedding)

    ordered_vectors: list[list[float]] = []
    for vector in vectors:
        if vector is None:
            msg = "Embedding batch did not fill every chunk-window vector"
            raise RuntimeError(msg)
        ordered_vectors.append(vector)

    return ordered_vectors, [metadata for _text, metadata in chunk_windows]


async def backfill_vector_store(
    database_dsn: str | None,
    qdrant_cfg: QdrantConfig,
    *,
    limit: int | None = None,
    force: bool = False,
    batch_size: int = 50,
    dry_run: bool = False,
) -> None:
    logger.info(
        "vector_backfill_start",
        extra={"explicit_dsn": database_dsn is not None, "limit": limit, "dry_run": dry_run},
    )

    app_cfg = load_config(allow_stub_telegram=True)
    db = Database(config=DatabaseConfig(dsn=database_dsn) if database_dsn else DatabaseConfig())
    try:
        embedding_repo = EmbeddingRepositoryAdapter(db)
        embedding_service = create_embedding_service(app_cfg.embedding)
        generator = SummaryEmbeddingGenerator(
            embedding_repository=embedding_repo,
            request_repository=RequestRepositoryAdapter(db),
            summary_repository=SummaryRepositoryAdapter(db),
            embedding_service=embedding_service,
            max_token_length=app_cfg.embedding.max_token_length,
        )

        summaries = await _fetch_summaries(db, limit)
        logger.info("vector_backfill_summaries_found", extra={"count": len(summaries)})
        existing_embeddings = await _fetch_embeddings_by_summary_id(
            embedding_repo,
            _summary_ids(summaries),
        )

        vector_store = QdrantVectorStore(
            url=qdrant_cfg.url,
            api_key=qdrant_cfg.api_key,
            environment=qdrant_cfg.environment,
            user_scope=qdrant_cfg.user_scope,
            collection_version=qdrant_cfg.collection_version,
            embedding_space=resolve_embedding_space_identifier(app_cfg.embedding),
            embedding_dim=app_cfg.embedding.embedding_dim,
            required=qdrant_cfg.required,
            connection_timeout=qdrant_cfg.connection_timeout,
        )

        processed = 0
        deleted = 0
        skipped = 0
        pending_requests: list[tuple[int, list[list[float]], list[dict[str, Any]]]] = []
        pending_vector_count = 0

        def flush_pending() -> None:
            nonlocal pending_vector_count
            if dry_run:
                logger.info(
                    "vector_backfill_dry_run_flush",
                    extra={"pending_requests": len(pending_requests)},
                )
                pending_requests.clear()
                pending_vector_count = 0
                return
            for pending_request_id, request_vectors, request_metadata in pending_requests:
                # Backfill is operator-rerunnable, so skip the per-request disk
                # flush; a lost write is recovered by re-running the backfill.
                vector_store.replace_request_notes(
                    pending_request_id,
                    request_vectors,
                    request_metadata,
                    wait=False,
                )
            pending_requests.clear()
            pending_vector_count = 0

        generated_summary_ids: list[int] = []
        for summary in summaries:
            summary_id = summary.get("id")
            request_id = summary.get("request_id")
            payload = summary.get("json_payload")
            language = summary.get("lang")

            if not summary_id or not request_id or not payload:
                continue

            if existing_embeddings.get(summary_id) and not force:
                continue

            await generator.generate_embedding_for_summary(
                summary_id=summary_id,
                payload=payload,
                language=language,
                force=force,
            )
            generated_summary_ids.append(summary_id)

        if generated_summary_ids:
            refreshed_embeddings = await _fetch_embeddings_by_summary_id(
                embedding_repo,
                generated_summary_ids,
            )
            existing_embeddings.update(refreshed_embeddings)

        for summary in summaries:
            summary_id = summary.get("id")
            request_id = summary.get("request_id")
            request_row = summary.get("request") if isinstance(summary.get("request"), dict) else {}
            user_id = request_row.get("user_id") if isinstance(request_row, dict) else None
            payload = summary.get("json_payload")
            language = summary.get("lang")

            if not summary_id or not request_id:
                continue

            if not payload:
                logger.info(
                    "vector_backfill_delete_empty_payload",
                    extra={"request_id": request_id, "summary_id": summary_id},
                )
                if not dry_run:
                    vector_store.delete_by_request_id(request_id)
                deleted += 1
                continue

            existing = existing_embeddings.get(summary_id)
            if not existing:
                logger.warning(
                    "vector_backfill_no_embedding",
                    extra={"summary_id": summary_id},
                )
                skipped += 1
                continue

            chunk_windows = MetadataBuilder.prepare_chunk_windows_for_upsert(
                request_id=request_id,
                summary_id=summary_id,
                payload=payload,
                language=language,
                user_scope=qdrant_cfg.user_scope,
                environment=qdrant_cfg.environment,
                user_id=user_id,
            )

            request_vectors: list[list[float]] = []
            request_metadata: list[dict[str, Any]] = []

            if chunk_windows:
                request_vectors, request_metadata = await _generate_chunk_window_vectors(
                    embedding_service,
                    chunk_windows,
                )
            else:
                text, metadata = MetadataBuilder.prepare_for_upsert(
                    request_id=request_id,
                    summary_id=summary_id,
                    payload=payload,
                    language=language,
                    user_scope=qdrant_cfg.user_scope,
                    environment=qdrant_cfg.environment,
                    user_id=user_id,
                    summary_row=summary,
                )

                if not text:
                    logger.info(
                        "vector_backfill_delete_empty_text",
                        extra={"request_id": request_id, "summary_id": summary_id},
                    )
                    if not dry_run:
                        vector_store.delete_by_request_id(request_id)
                    deleted += 1
                    continue

                embedding = embedding_service.deserialize_embedding(existing["embedding_blob"])
                vector = _as_vector(embedding)

                request_vectors.append(vector)
                request_metadata.append(metadata)

            if not request_vectors:
                logger.info(
                    "vector_backfill_delete_empty_vectors",
                    extra={"request_id": request_id, "summary_id": summary_id},
                )
                if not dry_run:
                    vector_store.delete_by_request_id(request_id)
                deleted += 1
                continue

            pending_requests.append((request_id, request_vectors, request_metadata))
            pending_vector_count += len(request_vectors)
            processed += len(request_vectors)

            if pending_vector_count >= batch_size:
                flush_pending()

        if pending_requests:
            flush_pending()

        logger.info(
            "vector_backfill_complete",
            extra={"processed": processed, "deleted": deleted, "skipped": skipped},
        )
    finally:
        await db.dispose()


def _load_qdrant_config(
    *,
    url: str | None,
    api_key: str | None,
    environment: str | None,
    user_scope: str | None,
    collection_version: str | None = None,
) -> QdrantConfig:
    base_cfg = load_config(allow_stub_telegram=True).vector_store
    return QdrantConfig(
        url=url or base_cfg.url,
        api_key=api_key if api_key is not None else base_cfg.api_key,
        environment=environment or base_cfg.environment,
        user_scope=user_scope or base_cfg.user_scope,
        collection_version=collection_version or base_cfg.collection_version,
        required=base_cfg.required,
        connection_timeout=base_cfg.connection_timeout,
    )


def main() -> int:
    database_dsn = None
    qdrant_url = None
    qdrant_api_key = None
    qdrant_env = None
    qdrant_scope = None
    qdrant_version = None
    limit = None
    force = False
    batch_size = 50
    dry_run = False

    args = sys.argv[1:]
    for arg in args:
        if arg.startswith("--dsn="):
            database_dsn = arg.split("=", 1)[1]
        elif arg.startswith("--db="):
            logger.error("--db is no longer supported; set DATABASE_URL or use --dsn=DSN")
            return 1
        elif arg.startswith("--qdrant-url="):
            qdrant_url = arg.split("=", 1)[1]
        elif arg.startswith("--qdrant-api-key="):
            qdrant_api_key = arg.split("=", 1)[1]
        elif arg.startswith("--qdrant-env="):
            qdrant_env = arg.split("=", 1)[1]
        elif arg.startswith("--qdrant-scope="):
            qdrant_scope = arg.split("=", 1)[1]
        elif arg.startswith("--qdrant-version="):
            qdrant_version = arg.split("=", 1)[1]
        elif arg.startswith("--limit="):
            try:
                limit = int(arg.split("=", 1)[1])
            except ValueError:
                logger.error("Invalid limit value: %s", arg)
                return 1
        elif arg == "--force":
            force = True
        elif arg == "--dry-run":
            dry_run = True
        elif arg.startswith("--batch-size="):
            try:
                batch_size = int(arg.split("=", 1)[1])
            except ValueError:
                logger.error("Invalid batch size: %s", arg)
                return 1
        elif arg in ("--help", "-h"):
            print("Usage: python -m app.cli.backfill_vector_store [OPTIONS]")
            print()
            print("Options:")
            print("  --dsn=DSN               PostgreSQL DSN (default: DATABASE_URL)")
            print("  --qdrant-url=URL        Qdrant URL (default from config)")
            print("  --qdrant-api-key=KEY    Qdrant API key (default from config)")
            print("  --qdrant-env=NAME       Environment namespace for the collection")
            print("  --qdrant-scope=NAME     User/tenant scope for the collection")
            print("  --qdrant-version=VER    Collection version suffix (default from config)")
            print("  --limit=N               Process only N summaries")
            print("  --force                 Regenerate embeddings even if they exist")
            print("  --dry-run               Simulate without writing to Qdrant")
            print("  --batch-size=N          Number of vectors per upsert batch (default: 50)")
            print("  --help, -h              Show this help message")
            return 0

    try:
        qdrant_cfg = _load_qdrant_config(
            url=qdrant_url,
            api_key=qdrant_api_key,
            environment=qdrant_env,
            user_scope=qdrant_scope,
            collection_version=qdrant_version,
        )
    except Exception:
        logger.exception("Failed to load Qdrant configuration")
        return 1

    try:
        asyncio.run(
            backfill_vector_store(
                database_dsn=database_dsn,
                qdrant_cfg=qdrant_cfg,
                limit=limit,
                force=force,
                batch_size=batch_size,
                dry_run=dry_run,
            )
        )
        return 0
    except KeyboardInterrupt:
        logger.info("Backfill interrupted by user")
        return 130
    except Exception:
        logger.exception("Backfill failed with error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
