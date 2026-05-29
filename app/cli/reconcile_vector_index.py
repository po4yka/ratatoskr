"""CLI tool to inspect and repair vector index consistency."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app.cli.backfill_repository_embeddings import backfill_repository_embeddings
from app.cli.backfill_vector_store import backfill_vector_store
from app.config import DatabaseConfig, load_config
from app.core.embedding_space import resolve_embedding_space_identifier
from app.db.session import Database
from app.infrastructure.embedding.embedding_service import DEFAULT_MODELS
from app.infrastructure.vector.reconciliation import (
    GitMirrorVectorIndexedEntityAdapter,
    RepositoryVectorIndexedEntityAdapter,
    SummaryVectorIndexedEntityAdapter,
    VectorIndexReconciler,
)


def _expected_models(cfg: Any) -> set[str]:
    if getattr(cfg.embedding, "provider", "local") == "gemini":
        return {str(cfg.embedding.gemini_model)}
    return set(DEFAULT_MODELS.values())


async def reconcile_vector_index(
    *,
    database_dsn: str | None = None,
    repair: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    cfg = load_config(allow_stub_telegram=True)
    db = Database(config=DatabaseConfig(dsn=database_dsn) if database_dsn else DatabaseConfig())
    vector_store = None
    try:
        from app.infrastructure.vector.qdrant_store import QdrantVectorStore

        vector_store = QdrantVectorStore(
            url=cfg.vector_store.url,
            api_key=cfg.vector_store.api_key,
            environment=cfg.vector_store.environment,
            user_scope=cfg.vector_store.user_scope,
            collection_version=cfg.vector_store.collection_version,
            embedding_space=resolve_embedding_space_identifier(cfg.embedding),
            embedding_dim=cfg.embedding.embedding_dim,
            required=cfg.vector_store.required,
            connection_timeout=cfg.vector_store.connection_timeout,
        )
        if not vector_store.available:
            vector_store = None
        expected_models = _expected_models(cfg)
        report = await VectorIndexReconciler(
            database=db,
            vector_store=vector_store,
            expected_summary_models=expected_models,
            expected_repository_models=expected_models,
            scan_limit=limit or cfg.vector_reconcile.batch_size,
            adapters=[
                SummaryVectorIndexedEntityAdapter(expected_models),
                RepositoryVectorIndexedEntityAdapter(expected_models),
                GitMirrorVectorIndexedEntityAdapter(),
            ],
        ).inspect()
        result: dict[str, Any] = {"report": report.to_diagnostics()}
    finally:
        await db.dispose()

    if repair:
        await backfill_vector_store(
            database_dsn=database_dsn,
            qdrant_cfg=cfg.vector_store,
            limit=limit,
            force=True,
            batch_size=cfg.vector_reconcile.batch_size,
            dry_run=dry_run,
        )
        result["repository_repair"] = await backfill_repository_embeddings(
            database_dsn=database_dsn,
            dry_run=dry_run,
            batch_size=cfg.vector_reconcile.batch_size,
            model_version_target="1.0",
        )
    return result


def main() -> int:
    database_dsn: str | None = None
    repair = False
    dry_run = False
    limit: int | None = None
    for arg in sys.argv[1:]:
        if arg.startswith("--dsn="):
            database_dsn = arg.split("=", 1)[1]
        elif arg == "--repair":
            repair = True
        elif arg == "--dry-run":
            dry_run = True
        elif arg.startswith("--limit="):
            try:
                limit = int(arg.split("=", 1)[1])
            except ValueError:
                print(f"Invalid limit: {arg}", file=sys.stderr)
                return 1
        elif arg in ("--help", "-h"):
            print("Usage: python -m app.cli.reconcile_vector_index [OPTIONS]")
            print()
            print("Options:")
            print("  --dsn=DSN      PostgreSQL DSN (default: DATABASE_URL)")
            print("  --repair       Run summary and repository vector backfills after reporting")
            print("  --dry-run      With --repair, do not write")
            print("  --limit=N      Limit reconciliation/backfill scan size")
            print("  --help, -h     Show this help message")
            return 0
        else:
            print(f"Unknown argument: {arg}", file=sys.stderr)
            return 1

    result = asyncio.run(
        reconcile_vector_index(
            database_dsn=database_dsn,
            repair=repair,
            dry_run=dry_run,
            limit=limit,
        )
    )
    print(json.dumps(result, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
