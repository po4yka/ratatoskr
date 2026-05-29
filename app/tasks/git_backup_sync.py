"""Taskiq task: periodic git mirror backup sync."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.tasks.broker import broker
from app.tasks.deps import get_app_config, get_db

logger = get_logger(__name__)

_GIT_BACKUP_SYNC_LOCK_KEY = "task_lock:git_backup_sync"
# TTL covers the maximum expected run for a large mirror set; 1 hour default.
_GIT_BACKUP_SYNC_LOCK_TTL = 3600


async def _enumerate_and_upsert_gists(cfg: AppConfig, db: Database) -> int:
    """Enumerate GitHub gists for all active integrations and upsert GitMirror rows.

    Returns the total number of gist mirrors upserted across all users.

    Per-user failures (GitHub API errors, token decryption failures) are logged
    and skipped — they must not abort the broader sync run.
    """
    from app.adapters.git_backup.repository import GitMirrorRepository
    from app.adapters.github.github_api_client import GitHubAPIClient
    from app.db.models.git_backup import GitMirrorSource
    from app.db.models.repository import GitHubIntegrationStatus, UserGitHubIntegration
    from app.security.secret_crypto import decrypt_secret

    async with db.session() as session:
        result = await session.execute(
            select(UserGitHubIntegration).where(
                UserGitHubIntegration.status == GitHubIntegrationStatus.ACTIVE
            )
        )
        integrations = list(result.scalars().all())

    mirror_repo = GitMirrorRepository(db, cfg.git_backup)
    total_upserted = 0

    for integration in integrations:
        user_id = integration.user_id
        try:
            token = decrypt_secret(integration.encrypted_token)
        except Exception as exc:
            logger.warning(
                "git_backup_gist_decrypt_failed",
                extra={"user_id": user_id, "error": str(exc)},
            )
            continue

        try:
            async with GitHubAPIClient(token) as client:
                gists = await client.list_gists()
        except Exception as exc:
            logger.warning(
                "git_backup_gist_api_error",
                extra={"user_id": user_id, "error": str(exc)},
            )
            continue

        for gist in gists:
            name = gist.description.strip() if gist.description else f"gist:{gist.id}"
            try:
                await mirror_repo.upsert_target(
                    user_id=user_id,
                    source=GitMirrorSource.GITHUB,
                    clone_url=gist.git_pull_url,
                    name=name,
                )
                total_upserted += 1
            except Exception as exc:
                logger.warning(
                    "git_backup_gist_upsert_failed",
                    extra={"user_id": user_id, "gist_id": gist.id, "error": str(exc)},
                )

        logger.debug(
            "git_backup_gist_enumerated",
            extra={"user_id": user_id, "count": len(gists)},
        )

    return total_upserted


async def _index_mirror_readmes(
    cfg: AppConfig,
    db: Database,
    summary: Any,
) -> None:
    """Index README content of successfully-synced non-GitHub mirrors into Qdrant.

    Only mirrors that (a) succeeded in this run, (b) have repository_id IS NULL,
    and (c) have a mirror_path on disk are processed.  Best-effort: any error
    per mirror is logged and swallowed inside the indexer.
    """
    from app.di.shared import build_qdrant_vector_store
    from app.infrastructure.embedding.embedding_factory import create_embedding_service
    from app.infrastructure.search.git_mirror_readme_indexer import GitMirrorReadmeIndexer

    try:
        embedding_service = create_embedding_service(cfg.embedding)
        qdrant_store = build_qdrant_vector_store(cfg)
    except Exception:
        logger.warning("git_backup_readme_index_infra_unavailable")
        return

    indexer = GitMirrorReadmeIndexer(
        embedding_service=embedding_service,
        qdrant_store=qdrant_store,
        db=db,
        environment=cfg.vector_store.environment,
        user_scope=cfg.vector_store.user_scope,
    )

    candidates: list[tuple[object, Path]] = []
    for outcome in summary.outcomes:
        if not outcome.ok:
            continue
        mirror = outcome.mirror
        # Only non-GitHub mirrors (repository_id IS NULL).
        if mirror.repository_id is not None:
            continue
        mirror_path = getattr(mirror, "mirror_path", None)
        if not mirror_path:
            continue
        p = Path(mirror_path)
        if not p.exists():
            continue
        candidates.append((mirror, p))

    if not candidates:
        logger.debug("git_backup_readme_index_no_candidates")
        return

    logger.info(
        "git_backup_readme_index_start",
        extra={"count": len(candidates)},
    )
    await indexer.index_mirrors(candidates)  # type: ignore[arg-type]
    logger.info(
        "git_backup_readme_index_done",
        extra={"count": len(candidates)},
    )


@broker.task(task_name="ratatoskr.git_backup.sync")
async def sync_git_backup(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> None:
    """Mirror all due git repositories to local bare-clone storage."""
    if not cfg.git_backup.enabled:
        logger.info("git_backup_sync_disabled")
        return

    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(
        redis_client, _GIT_BACKUP_SYNC_LOCK_KEY, _GIT_BACKUP_SYNC_LOCK_TTL
    ) as acquired:
        if not acquired:
            logger.info(
                "git_backup_sync_skipped_lock_held",
                extra={"key": _GIT_BACKUP_SYNC_LOCK_KEY},
            )
            return

        from app.adapters.git_backup.health_ping import (
            ping_failure,
            ping_start,
            ping_success,
        )
        from app.tasks.deps import build_git_backup_task_runtime

        hc_url = cfg.git_backup.hc_ping_url
        hc_timeout = cfg.git_backup.hc_ping_timeout_seconds

        if hc_url:
            await ping_start(hc_url, hc_timeout)

        try:
            # Enumerate gists first so freshly-upserted rows are picked up by
            # perform_sync in the same run (list_due includes PENDING status).
            if cfg.git_backup.mirror_gists:
                gists_upserted = await _enumerate_and_upsert_gists(cfg, db)
                logger.info(
                    "git_backup_gists_upserted",
                    extra={"count": gists_upserted},
                )

            runtime = build_git_backup_task_runtime(cfg, db)
            summary = await runtime.service.perform_sync()
        except Exception:
            if hc_url:
                await ping_failure(hc_url, hc_timeout)
            raise

        if hc_url:
            await ping_success(hc_url, hc_timeout)

        logger.info(
            "git_backup_sync_complete",
            extra={
                "ok": summary.ok,
                "failed": summary.failed,
                "skipped": summary.skipped,
                "total": summary.total,
            },
        )

        # README semantic indexing — best-effort, never blocks or fails the task.
        if cfg.git_backup.index_readmes:
            await _index_mirror_readmes(cfg, db, summary)
