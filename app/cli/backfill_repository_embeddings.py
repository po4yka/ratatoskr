"""CLI tool to backfill embeddings for GitHub repository entities."""

from __future__ import annotations

import asyncio
import sys
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import or_, select

from app.config import DatabaseConfig, load_config
from app.core.embedding_space import resolve_embedding_space_identifier
from app.core.logging_utils import get_logger
from app.core.repo_analysis_schema import RepoAnalysis
from app.db.models.repository import Repository, RepositoryEmbedding
from app.db.session import Database
from app.infrastructure.embedding.embedding_factory import create_embedding_service
from app.infrastructure.embedding.repository_embedding import (
    RepositoryEmbeddingBatchItem,
    RepositoryEmbeddingGenerator,
)

if TYPE_CHECKING:
    from app.infrastructure.vector.qdrant_store import QdrantVectorStore

logger = get_logger(__name__)


async def backfill_repository_embeddings(
    *,
    database_dsn: str | None = None,
    dry_run: bool = False,
    batch_size: int = 50,
    model_version_target: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Backfill missing or stale repository embeddings.

    Returns a summary dict with counters.
    """
    logger.info(
        "repo_embedding_backfill_start",
        extra={
            "dry_run": dry_run,
            "batch_size": batch_size,
            "model_version_target": model_version_target,
            "user_id": user_id,
        },
    )

    app_cfg = load_config(allow_stub_telegram=True)
    db = Database(config=DatabaseConfig(dsn=database_dsn) if database_dsn else DatabaseConfig())

    try:
        embedding_service = create_embedding_service(app_cfg.embedding)
        qdrant_cfg = app_cfg.vector_store

        qdrant_store: QdrantVectorStore | None = None
        if not dry_run:
            from app.infrastructure.vector.qdrant_store import QdrantVectorStore

            qdrant_store = QdrantVectorStore(
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

        embedding_gen = RepositoryEmbeddingGenerator(
            embedding_service=embedding_service,
            qdrant_store=qdrant_store,
            db=db,
            environment=qdrant_cfg.environment,
            user_scope=qdrant_cfg.user_scope,
        )

        processed = 0
        embeddings_created = 0
        embeddings_refreshed = 0
        skipped = 0
        errors = 0
        would_create = 0

        last_seen_id = 0
        while True:
            async with db.session() as session:
                # LEFT OUTER JOIN: include repos regardless of embedding presence
                stmt = (
                    select(Repository, RepositoryEmbedding)
                    .outerjoin(
                        RepositoryEmbedding,
                        RepositoryEmbedding.repository_id == Repository.id,
                    )
                    .where(Repository.id > last_seen_id)
                    .order_by(Repository.id.asc())
                    .limit(batch_size)
                )

                needs_repair = or_(
                    RepositoryEmbedding.id.is_(None),
                    RepositoryEmbedding.index_status != "indexed",
                    RepositoryEmbedding.last_indexed_at.is_(None),
                    RepositoryEmbedding.last_indexed_at < Repository.updated_at,
                )

                # WHERE: missing/failed/stale Qdrant indexing OR version mismatch.
                if model_version_target is not None:
                    stmt = stmt.where(
                        needs_repair
                        | (RepositoryEmbedding.model_version != model_version_target)
                    )
                else:
                    stmt = stmt.where(needs_repair)

                if user_id is not None:
                    stmt = stmt.where(Repository.user_id == user_id)

                rows = (await session.execute(stmt)).all()

            if not rows:
                break

            last_seen_id = rows[-1][0].id

            batch_items: list[RepositoryEmbeddingBatchItem] = []
            missing_by_repository_id: dict[int, bool] = {}

            for repo, existing_embedding in rows:
                repo_id = repo.id
                is_missing = existing_embedding is None

                if dry_run:
                    would_create += 1
                    logger.info(
                        "repo_embedding_backfill_dry_run_row",
                        extra={
                            "repository_id": repo_id,
                            "full_name": repo.full_name,
                            "status": "missing" if is_missing else "version_mismatch",
                        },
                    )
                    processed += 1
                    continue

                # Deserialize analysis_json -> RepoAnalysis | None
                analysis: RepoAnalysis | None = None
                if repo.analysis_json is not None:
                    try:
                        analysis = RepoAnalysis.model_validate(repo.analysis_json)
                    except Exception:
                        logger.warning(
                            "repo_embedding_backfill_invalid_analysis",
                            extra={"repository_id": repo_id},
                        )

                correlation_id = str(uuid.uuid4())
                batch_items.append(
                    RepositoryEmbeddingBatchItem(
                        repository=repo,
                        analysis=analysis,
                        correlation_id=correlation_id,
                    )
                )
                missing_by_repository_id[repo_id] = is_missing

            if batch_items:
                try:
                    batch_result = await embedding_gen.regenerate_batch(batch_items)
                except Exception:
                    logger.exception(
                        "repo_embedding_backfill_batch_error",
                        extra={"count": len(batch_items)},
                    )
                    for item in batch_items:
                        repo = item.repository
                        try:
                            await embedding_gen.regenerate(
                                repo,
                                analysis=item.analysis,
                                correlation_id=item.correlation_id,
                            )
                        except Exception:
                            logger.exception(
                                "repo_embedding_backfill_row_error",
                                extra={"repository_id": repo.id, "full_name": repo.full_name},
                            )
                            errors += 1
                            continue

                        if missing_by_repository_id.get(repo.id, False):
                            embeddings_created += 1
                        else:
                            embeddings_refreshed += 1
                else:
                    for success in batch_result.successes:
                        if missing_by_repository_id.get(success.repository_id, False):
                            embeddings_created += 1
                        else:
                            embeddings_refreshed += 1

                    for failure in batch_result.failures:
                        logger.error(
                            "repo_embedding_backfill_row_error",
                            exc_info=failure.error,
                            extra={
                                "repository_id": failure.repository_id,
                                "full_name": failure.full_name,
                            },
                        )
                        errors += 1

                processed += len(batch_items)

            if len(rows) < batch_size:
                break

        summary: dict[str, Any] = {
            "processed": processed,
            "embeddings_created": embeddings_created,
            "embeddings_refreshed": embeddings_refreshed,
            "skipped": skipped,
            "errors": errors,
            "dry_run": dry_run,
        }
        if dry_run:
            summary["would_create"] = would_create

        logger.info("repo_embedding_backfill_complete", extra=summary)
        return summary

    finally:
        await db.dispose()


def main() -> int:
    database_dsn: str | None = None
    dry_run = False
    batch_size = 50
    model_version_target: str | None = None
    user_id: int | None = None

    args = sys.argv[1:]
    for arg in args:
        if arg.startswith("--dsn="):
            database_dsn = arg.split("=", 1)[1]
        elif arg == "--dry-run":
            dry_run = True
        elif arg.startswith("--batch-size="):
            try:
                batch_size = int(arg.split("=", 1)[1])
            except ValueError:
                logger.error("Invalid batch-size value: %s", arg)
                return 1
        elif arg.startswith("--model-version-target="):
            model_version_target = arg.split("=", 1)[1]
        elif arg.startswith("--user-id="):
            try:
                user_id = int(arg.split("=", 1)[1])
            except ValueError:
                logger.error("Invalid user-id value: %s", arg)
                return 1
        elif arg.startswith("--log-level="):
            pass  # accepted but loguru level is not changed at runtime here
        elif arg.startswith("--env-file="):
            pass  # env file handled externally via dotenv at import time
        elif arg in ("--help", "-h"):
            print("Usage: python -m app.cli.backfill_repository_embeddings [OPTIONS]")
            print()
            print("Options:")
            print("  --dsn=DSN                     PostgreSQL DSN (default: DATABASE_URL)")
            print("  --dry-run                     Print what would be done, no writes")
            print("  --batch-size=N                DB iteration page size (default: 50)")
            print("  --model-version-target=VER    Re-embed rows with model_version != VER")
            print("  --user-id=ID                  Restrict to one user_id")
            print("  --log-level=LEVEL             Logging level (default: INFO)")
            print("  --env-file=FILE               .env file path (default: .env)")
            print("  --help, -h                    Show this help message")
            return 0
        else:
            logger.error("Unknown argument: %s", arg)
            return 1

    try:
        summary = asyncio.run(
            backfill_repository_embeddings(
                database_dsn=database_dsn,
                dry_run=dry_run,
                batch_size=batch_size,
                model_version_target=model_version_target,
                user_id=user_id,
            )
        )
        for key, value in summary.items():
            print(f"{key}={value}")
        return 0
    except KeyboardInterrupt:
        logger.info("Backfill interrupted by user")
        return 130
    except Exception:
        logger.exception("Backfill failed with error")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
