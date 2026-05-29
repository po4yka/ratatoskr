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


async def _enumerate_and_upsert_github_repos(cfg: AppConfig, db: Database) -> int:
    """Enumerate starred/owned/watched GitHub repos for all active integrations and upsert GitMirror rows.

    Returns the total number of mirror rows upserted across all users and repo categories.

    Behaviour:
    - Only the categories enabled in cfg.git_backup (mirror_starred / mirror_owned /
      mirror_watched) are fetched; the others are skipped entirely.
    - De-duplication is by clone_url within each user's batch — a repo that appears in
      multiple lists (e.g. both starred and owned) is upserted only once.
    - Per-user failures (API errors, decryption failures) are logged and skipped; they
      must not abort the broader sync run.
    - For each repo the GitHub-reported ``size`` (already in KB) is passed as size_kb so
      the large-repo timeout multiplier applies on the first clone without waiting for an
      on-disk measurement.
    - A ``repository_id`` FK is set when a matching ``repositories`` row already exists
      for the (user_id, github_id) pair; otherwise it is left NULL (the repo wil be
      indexed via the README path if index_readmes is enabled).
    """
    from app.adapters.git_backup.repository import GitMirrorRepository
    from app.adapters.github.github_api_client import GitHubAPIClient
    from app.db.models.git_backup import GitMirrorSource
    from app.db.models.repository import GitHubIntegrationStatus, Repository, UserGitHubIntegration
    from app.security.secret_crypto import decrypt_secret

    git_cfg = cfg.git_backup

    async with db.session() as session:
        result = await session.execute(
            select(UserGitHubIntegration).where(
                UserGitHubIntegration.status == GitHubIntegrationStatus.ACTIVE
            )
        )
        integrations = list(result.scalars().all())

    mirror_repo = GitMirrorRepository(db, git_cfg)
    total_upserted = 0

    for integration in integrations:
        user_id = integration.user_id
        try:
            token = decrypt_secret(integration.encrypted_token)
        except Exception as exc:
            logger.warning(
                "git_backup_repo_enum_decrypt_failed",
                extra={"user_id": user_id, "error": str(exc)},
            )
            continue

        # Collect repos from whichever categories are enabled, deduplicated by clone_url.
        # Values are RepositoryDTO instances; untyped dict avoids a TC001 violation
        # (the annotation would require a local TYPE_CHECKING import inside a function body).
        repos_by_clone_url: dict = {}

        try:
            async with GitHubAPIClient(token) as client:
                if git_cfg.mirror_starred:
                    async for item in await client.list_starred():
                        clone_url = f"https://github.com/{item.repo.full_name}.git"
                        if clone_url not in repos_by_clone_url:
                            repos_by_clone_url[clone_url] = item.repo

                if git_cfg.mirror_owned:
                    for owned_repo in await client.list_owned_repos():
                        clone_url = f"https://github.com/{owned_repo.full_name}.git"
                        if clone_url not in repos_by_clone_url:
                            repos_by_clone_url[clone_url] = owned_repo

                if git_cfg.mirror_watched:
                    for watched_repo in await client.list_watched_repos():
                        clone_url = f"https://github.com/{watched_repo.full_name}.git"
                        if clone_url not in repos_by_clone_url:
                            repos_by_clone_url[clone_url] = watched_repo
        except Exception as exc:
            logger.warning(
                "git_backup_repo_enum_api_error",
                extra={"user_id": user_id, "error": str(exc)},
            )
            continue

        # Build a lookup of github_id -> repositories.id for this user so we can
        # link the FK when the repo has already been ingested by the GitHub subsystem.
        github_ids = [repo_dto.id for repo_dto in repos_by_clone_url.values()]
        repo_id_by_github_id: dict[int, int] = {}
        if github_ids:
            async with db.session() as session:
                result = await session.execute(
                    select(Repository.id, Repository.github_id).where(
                        Repository.user_id == user_id,
                        Repository.github_id.in_(github_ids),
                    )
                )
                for row in result.all():
                    repo_id_by_github_id[row.github_id] = row.id

        for clone_url, repo_dto in repos_by_clone_url.items():
            try:
                repository_id = repo_id_by_github_id.get(repo_dto.id)
                await mirror_repo.upsert_target(
                    user_id=user_id,
                    source=GitMirrorSource.GITHUB,
                    clone_url=clone_url,
                    name=repo_dto.full_name,
                    size_kb=repo_dto.size if repo_dto.size else None,
                    repository_id=repository_id,
                )
                total_upserted += 1
            except Exception as exc:
                logger.warning(
                    "git_backup_repo_upsert_failed",
                    extra={
                        "user_id": user_id,
                        "clone_url": clone_url,
                        "error": str(exc),
                    },
                )

        logger.debug(
            "git_backup_repos_enumerated",
            extra={"user_id": user_id, "count": len(repos_by_clone_url)},
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


async def _prune_stale_excluded(cfg: AppConfig, db: Database) -> None:
    """Delete stale EXCLUDED mirrors: Qdrant point, on-disk dir, and DB row.

    Only runs when ``GIT_BACKUP_PRUNE_EXCLUDED_DAYS > 0``.  Each step is
    best-effort: errors are logged and the sweep continues to the next mirror.
    On-disk removal uses the same path-safety check as the DELETE endpoint:
    mirror_path must resolve strictly inside GIT_BACKUP_DATA_PATH.
    """
    import asyncio
    import shutil
    from pathlib import Path

    from app.adapters.git_backup.repository import GitMirrorRepository

    days = cfg.git_backup.prune_excluded_days
    if days <= 0:
        return

    mirror_repo = GitMirrorRepository(db, cfg.git_backup)
    stale = await mirror_repo.list_stale_excluded(days)
    if not stale:
        logger.debug("git_backup_prune_excluded_none")
        return

    logger.info("git_backup_prune_excluded_start", extra={"count": len(stale)})

    data_root = Path(cfg.git_backup.data_path).resolve()

    # Build Qdrant store once (best-effort; None means unavailable).
    qdrant_store = None
    try:
        from app.di.shared import build_qdrant_vector_store

        qdrant_store = build_qdrant_vector_store(cfg)
        if not qdrant_store.available:
            qdrant_store = None
    except Exception as exc:
        logger.warning("git_backup_prune_excluded_qdrant_unavailable", extra={"error": str(exc)})

    pruned = 0
    for mirror in stale:
        mirror_id = mirror.id
        mirror_path_str = mirror.mirror_path or ""

        # 1. Delete Qdrant point best-effort.
        if qdrant_store is not None:
            try:
                await asyncio.to_thread(
                    qdrant_store.delete_git_mirror_points, [mirror_id]
                )
            except Exception as exc:
                logger.warning(
                    "git_backup_prune_excluded_qdrant_failed",
                    extra={"mirror_id": mirror_id, "error": str(exc)},
                )

        # 2. Remove the on-disk bare clone best-effort (path-safety check).
        if mirror_path_str:
            try:
                target = Path(mirror_path_str).resolve()
                if target != data_root and target.is_relative_to(data_root):

                    def _rmtree(p: Path) -> None:
                        if p.exists():
                            shutil.rmtree(p)

                    await asyncio.to_thread(_rmtree, target)
                    logger.debug(
                        "git_backup_prune_excluded_disk_removed",
                        extra={"mirror_id": mirror_id, "path": str(target)},
                    )
                else:
                    logger.warning(
                        "git_backup_prune_excluded_disk_skipped_unsafe_path",
                        extra={
                            "mirror_id": mirror_id,
                            "mirror_path": mirror_path_str,
                            "data_root": str(data_root),
                        },
                    )
            except Exception as exc:
                logger.warning(
                    "git_backup_prune_excluded_disk_failed",
                    extra={"mirror_id": mirror_id, "mirror_path": mirror_path_str, "error": str(exc)},
                )

        # 3. Delete the DB row best-effort.
        try:
            await mirror_repo.delete_mirror(mirror_id)
            pruned += 1
        except Exception as exc:
            logger.warning(
                "git_backup_prune_excluded_db_failed",
                extra={"mirror_id": mirror_id, "error": str(exc)},
            )

    logger.info("git_backup_prune_excluded_done", extra={"pruned": pruned, "total": len(stale)})


async def _reconcile_mirror_readmes(cfg: AppConfig, db: Database) -> None:
    """Reconcile git_mirror README vectors against the DB (best-effort).

    Deletes orphaned Qdrant points and recreates missing ones (force re-index).
    Reuses the same embedding + Qdrant infra as the indexing pass.
    """
    from app.di.shared import build_qdrant_vector_store
    from app.infrastructure.embedding.embedding_factory import create_embedding_service
    from app.infrastructure.search.git_mirror_readme_indexer import GitMirrorReadmeIndexer
    from app.infrastructure.search.git_mirror_reconciler import GitMirrorVectorReconciler

    try:
        embedding_service = create_embedding_service(cfg.embedding)
        qdrant_store = build_qdrant_vector_store(cfg)
    except Exception:
        logger.warning("git_backup_reconcile_infra_unavailable")
        return

    indexer = GitMirrorReadmeIndexer(
        embedding_service=embedding_service,
        qdrant_store=qdrant_store,
        db=db,
        environment=cfg.vector_store.environment,
        user_scope=cfg.vector_store.user_scope,
    )
    reconciler = GitMirrorVectorReconciler(db=db, qdrant_store=qdrant_store, indexer=indexer)
    await reconciler.reconcile_and_repair()


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

            # Enumerate starred/owned/watched repos when any of the flags are on.
            if (
                cfg.git_backup.mirror_starred
                or cfg.git_backup.mirror_owned
                or cfg.git_backup.mirror_watched
            ):
                repos_upserted = await _enumerate_and_upsert_github_repos(cfg, db)
                logger.info(
                    "git_backup_repos_upserted",
                    extra={"count": repos_upserted},
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

        # README vector reconciliation — best-effort, never blocks or fails the task.
        if cfg.git_backup.reconcile_readmes:
            await _reconcile_mirror_readmes(cfg, db)

        # Stale-EXCLUDED prune sweep — best-effort, never blocks or fails the task.
        if cfg.git_backup.prune_excluded_days > 0:
            try:
                await _prune_stale_excluded(cfg, db)
            except Exception:
                logger.warning("git_backup_prune_excluded_unexpected_error")
