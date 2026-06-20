"""Taskiq task: daily GitHub starred-repository sync."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select, update
from taskiq import TaskiqDepends

from app.adapters.content.streaming.operation_streams import (
    github_sync_topic,
    publish_operation_event,
)
from app.adapters.github.exceptions import GitHubAuthError, GitHubRateLimitError
from app.adapters.github.github_api_client import GitHubAPIClient
from app.application.events.repository_watch import RepositoryWatchTriggered
from app.application.use_cases.analyze_repository import _compute_content_hash
from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.db.models.repository import (
    GitHubIntegrationStatus,
    Repository,
    RepoSource,
    UserGitHubIntegration,
    UserRepositoryWatch,
)
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.observability.metrics_repositories import (
    GITHUB_PENDING_ANALYSIS_BACKLOG,
    GITHUB_REPOSITORY_WATCH_TRIGGERS_TOTAL,
    GITHUB_SYNC_LLM_CALLS_TOTAL,
    GITHUB_SYNC_RATE_LIMIT_STREAK,
    GITHUB_SYNC_RATE_LIMITED_TOTAL,
    GITHUB_SYNC_REPOS_IMPORTED_TOTAL,
    GITHUB_SYNC_REPOS_UNSTARRED_TOTAL,
    GITHUB_SYNC_REPOS_UPDATED_TOTAL,
    GITHUB_SYNC_RUNS_TOTAL,
)
from app.security.token_crypto import decrypt_token
from app.tasks.broker import broker
from app.tasks.deps import create_digest_bot_client, get_app_config, get_db

logger = get_logger(__name__)


@dataclass
class SyncSummary:
    """Aggregated result of a full github stars sync run."""

    users_processed: int
    repos_imported: int
    repos_updated: int
    repos_unstarred: int
    llm_calls_made: int
    llm_calls_deferred: int
    errors_per_user: dict[int, str] = field(default_factory=dict)


_GITHUB_SYNC_LOCK_KEY = "task_lock:github_sync_stars"
# TTL covers the maximum expected run: 100-repo budget * ~18 s/LLM call ≈ 30 min.
_GITHUB_SYNC_LOCK_TTL = 1800


@broker.task(task_name="ratatoskr.github.sync_stars", retry_on_error=True, max_retries=3)
async def sync_all_active_integrations(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> SyncSummary:
    """Poll GitHub starred repos for all active integrations and upsert into DB."""
    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(
        redis_client, _GITHUB_SYNC_LOCK_KEY, _GITHUB_SYNC_LOCK_TTL
    ) as acquired:
        if not acquired:
            logger.info(
                "github_sync_skipped_lock_held",
                extra={"key": _GITHUB_SYNC_LOCK_KEY},
            )
            return SyncSummary(
                users_processed=0,
                repos_imported=0,
                repos_updated=0,
                repos_unstarred=0,
                llm_calls_made=0,
                llm_calls_deferred=0,
            )
        return await _sync_body_with_bot(cfg, db)


async def _sync_body_with_bot(cfg: AppConfig, db: Database) -> SyncSummary:
    """Run the sync with a worker-side Telethon bot for needs_reauth DMs.

    The Taskiq worker shares no Telethon state with the long-running bot
    process, so it builds its own short-lived bot client (the pattern used by
    the digest and git-backup tasks) solely to deliver the needs_reauth owner
    DM. Best-effort: if the bot cannot be built or connected, the sync still
    runs with bot=None and the DM is skipped — a Telethon problem must never
    break the GitHub sync.
    """
    async with AsyncExitStack() as stack:
        bot: Any = None
        try:
            bot = await stack.enter_async_context(create_digest_bot_client(cfg))
        except Exception:
            logger.warning("github_sync_bot_client_unavailable")
            bot = None
        return await _sync_body(cfg, db, bot=bot)


async def _sync_body(
    cfg: AppConfig,
    db: Database,
    *,
    bot: Any = None,
) -> SyncSummary:
    """Core sync logic — separated for direct testability."""
    correlation_id = f"github-sync-{uuid4()}"
    if not cfg.github.sync_enabled:
        logger.info("github_sync_disabled", extra={"cid": correlation_id})
        return SyncSummary(
            users_processed=0,
            repos_imported=0,
            repos_updated=0,
            repos_unstarred=0,
            llm_calls_made=0,
            llm_calls_deferred=0,
        )

    logger.info("github_sync_starting", extra={"cid": correlation_id})

    async with db.session() as session:
        result = await session.execute(
            select(UserGitHubIntegration).where(
                UserGitHubIntegration.status == GitHubIntegrationStatus.ACTIVE
            )
        )
        integrations: list[UserGitHubIntegration] = list(result.scalars().all())

    return await _sync_all(
        integrations,
        cfg=cfg,
        db=db,
        bot=bot,
        correlation_id=correlation_id,
    )


async def _sync_all(
    integrations: list[UserGitHubIntegration],
    *,
    cfg: AppConfig,
    db: Database,
    bot: Any = None,
    correlation_id: str | None = None,
    dry_run: bool = False,
) -> SyncSummary:
    """Sync loop over a pre-filtered list of integrations.

    Exposed so the CLI can pass a subset (e.g. filtered by user_id) and
    set *dry_run=True* without touching the Taskiq task signature.
    """
    if correlation_id is None:
        correlation_id = f"github-sync-{uuid4()}"

    _publish_github_sync_event(
        correlation_id,
        "phase",
        {"phase": "starting", "integrations": len(integrations)},
    )

    total_imported = 0
    total_updated = 0
    total_unstarred = 0
    total_llm_made = 0
    total_llm_deferred = 0
    errors_per_user: dict[int, str] = {}
    rate_limited_users: set[int] = set()
    users_processed = 0

    for integration in integrations:
        users_processed += 1
        state = _github_sync_state(integration)
        backoff_until = state.get("backoff_until")
        if isinstance(backoff_until, datetime) and backoff_until > datetime.now(UTC):
            errors_per_user[integration.user_id] = "backoff_active"
            continue

        try:
            (
                imported,
                updated,
                unstarred,
                llm_made,
                llm_deferred,
            ) = await _sync_one_integration(
                integration=integration,
                cfg=cfg,
                db=db,
                bot=bot,
                correlation_id=correlation_id,
                dry_run=dry_run,
            )
            total_imported += imported
            total_updated += updated
            total_unstarred += unstarred
            total_llm_made += llm_made
            total_llm_deferred += llm_deferred
            _publish_github_sync_event(
                correlation_id,
                "repos_fetched",
                {
                    "user_id": integration.user_id,
                    "repos_imported": imported,
                    "repos_updated": updated,
                    "repos_unstarred": unstarred,
                    "users_processed": users_processed,
                },
            )
            _publish_github_sync_event(
                correlation_id,
                "repos_analyzed",
                {
                    "user_id": integration.user_id,
                    "llm_calls_made": llm_made,
                    "llm_calls_deferred": llm_deferred,
                },
            )
            if GITHUB_SYNC_RATE_LIMIT_STREAK is not None and not dry_run:
                GITHUB_SYNC_RATE_LIMIT_STREAK.labels(user_id=str(integration.user_id)).set(0)

        except GitHubAuthError as exc:
            logger.warning(
                "github_sync_auth_error",
                extra={"cid": correlation_id, "user_id": integration.user_id, "error": str(exc)},
            )
            errors_per_user[integration.user_id] = str(exc)
            _publish_github_sync_event(
                correlation_id,
                "phase",
                {"user_id": integration.user_id, "phase": "auth", "message": str(exc)},
            )
            async with db.transaction() as session:
                row = await session.get(UserGitHubIntegration, integration.id)
                if row is not None:
                    row.status = GitHubIntegrationStatus.NEEDS_REAUTH
                    row.last_sync_cursor = _github_sync_error_payload(
                        error=str(exc),
                        failure_count=_github_failure_count(row) + 1,
                    )
            if GITHUB_SYNC_RATE_LIMIT_STREAK is not None and not dry_run:
                GITHUB_SYNC_RATE_LIMIT_STREAK.labels(user_id=str(integration.user_id)).set(0)
            await _notify_needs_reauth(
                integration=integration,
                bot=bot,
                db=db,
                correlation_id=correlation_id,
            )

        except GitHubRateLimitError as exc:
            logger.warning(
                "github_sync_rate_limit",
                extra={
                    "cid": correlation_id,
                    "user_id": integration.user_id,
                    "reset_epoch": exc.reset_epoch,
                },
            )
            errors_per_user[integration.user_id] = f"rate_limit reset={exc.reset_epoch}"
            _publish_github_sync_event(
                correlation_id,
                "phase",
                {
                    "user_id": integration.user_id,
                    "phase": "fetching",
                    "message": f"rate_limit reset={exc.reset_epoch}",
                },
            )
            rate_limited_users.add(integration.user_id)
            rate_limit_streak = await _record_github_sync_error(
                db,
                integration_id=integration.id,
                error=f"rate_limit reset={exc.reset_epoch}",
                backoff_until=datetime.fromtimestamp(exc.reset_epoch, tz=UTC)
                if exc.reset_epoch is not None
                else None,
                rate_limited=True,
            )
            if GITHUB_SYNC_RATE_LIMITED_TOTAL is not None and not dry_run:
                GITHUB_SYNC_RATE_LIMITED_TOTAL.labels(user_id=str(integration.user_id)).inc()
            if GITHUB_SYNC_RATE_LIMIT_STREAK is not None and not dry_run:
                GITHUB_SYNC_RATE_LIMIT_STREAK.labels(user_id=str(integration.user_id)).set(
                    rate_limit_streak
                )

        except Exception as exc:
            logger.exception(
                "github_sync_user_error",
                extra={"cid": correlation_id, "user_id": integration.user_id, "error": str(exc)},
            )
            errors_per_user[integration.user_id] = str(exc)
            _publish_github_sync_event(
                correlation_id,
                "phase",
                {"user_id": integration.user_id, "phase": "syncing", "message": str(exc)},
            )
            await _record_github_sync_error(
                db,
                integration_id=integration.id,
                error=str(exc),
            )
            if GITHUB_SYNC_RATE_LIMIT_STREAK is not None and not dry_run:
                GITHUB_SYNC_RATE_LIMIT_STREAK.labels(user_id=str(integration.user_id)).set(0)

    summary = SyncSummary(
        users_processed=users_processed,
        repos_imported=total_imported,
        repos_updated=total_updated,
        repos_unstarred=total_unstarred,
        llm_calls_made=total_llm_made,
        llm_calls_deferred=total_llm_deferred,
        errors_per_user=errors_per_user,
    )
    logger.info(
        "github_sync_complete",
        extra={
            "cid": correlation_id,
            "users_processed": users_processed,
            "repos_imported": total_imported,
            "repos_updated": total_updated,
            "repos_unstarred": total_unstarred,
            "llm_calls_made": total_llm_made,
            "llm_calls_deferred": total_llm_deferred,
            "errors": len(errors_per_user),
        },
    )
    _publish_github_sync_event(
        correlation_id,
        "done" if not errors_per_user else "error",
        {
            "users_processed": users_processed,
            "repos_imported": total_imported,
            "repos_updated": total_updated,
            "repos_unstarred": total_unstarred,
            "llm_calls_made": total_llm_made,
            "llm_calls_deferred": total_llm_deferred,
            "errors": errors_per_user,
        },
    )

    # Prometheus counters
    if GITHUB_SYNC_REPOS_IMPORTED_TOTAL is not None and total_imported > 0:
        GITHUB_SYNC_REPOS_IMPORTED_TOTAL.inc(total_imported)
    if GITHUB_SYNC_REPOS_UPDATED_TOTAL is not None and total_updated > 0:
        GITHUB_SYNC_REPOS_UPDATED_TOTAL.inc(total_updated)
    if GITHUB_SYNC_REPOS_UNSTARRED_TOTAL is not None and total_unstarred > 0:
        GITHUB_SYNC_REPOS_UNSTARRED_TOTAL.inc(total_unstarred)
    if GITHUB_SYNC_LLM_CALLS_TOTAL is not None:
        if total_llm_made > 0:
            GITHUB_SYNC_LLM_CALLS_TOTAL.labels(trigger="made").inc(total_llm_made)
        if total_llm_deferred > 0:
            GITHUB_SYNC_LLM_CALLS_TOTAL.labels(trigger="deferred").inc(total_llm_deferred)
    if GITHUB_SYNC_RUNS_TOTAL is not None:
        if not errors_per_user:
            run_status = "ok"
        elif rate_limited_users and rate_limited_users == set(errors_per_user):
            run_status = "ratelimited"
        elif len(errors_per_user) < users_processed:
            run_status = "partial"
        else:
            run_status = "failed"
        GITHUB_SYNC_RUNS_TOTAL.labels(status=run_status).inc()
    if GITHUB_PENDING_ANALYSIS_BACKLOG is not None and not dry_run:
        # A metrics read must never break the sync run, so swallow any DB error.
        try:
            async with db.session() as session:
                # Intentionally global (no user_id filter): this is a Prometheus
                # system-health gauge reflecting the total pending-analysis backlog
                # across the entire single-tenant deployment. Adding a per-user
                # predicate would undercount and misrepresent the real queue depth.
                backlog = await session.execute(
                    select(func.count(Repository.id)).where(
                        Repository.pending_analysis == True  # noqa: E712
                    )
                )
                GITHUB_PENDING_ANALYSIS_BACKLOG.set(backlog.scalar_one() or 0)
        except Exception:
            logger.warning(
                "github_pending_backlog_gauge_failed",
                extra={"cid": correlation_id},
            )

    return summary


def _publish_github_sync_event(
    correlation_id: str,
    kind: str,
    payload: dict[str, Any],
) -> None:
    publish_operation_event(
        topic=github_sync_topic(correlation_id),
        kind=kind,
        correlation_id=correlation_id,
        payload=payload,
    )


async def _sync_one_integration(
    *,
    integration: UserGitHubIntegration,
    cfg: AppConfig,
    db: Database,
    bot: Any,
    correlation_id: str,
    dry_run: bool = False,
) -> tuple[int, int, int, int, int]:
    """Sync a single user's starred repos.

    Returns (imported, updated, unstarred, llm_made, llm_deferred).

    When *dry_run* is True, no DB writes or Qdrant mutations are performed;
    counts reflect what *would* have been written.
    """
    token = decrypt_token(integration.encrypted_token)

    repos_imported = 0
    repos_updated = 0
    repos_to_analyze: list[Repository] = []
    seen_github_ids: set[int] = set()

    is_first_sync = integration.last_synced_at is None
    is_full_star_snapshot = integration.last_synced_at is None

    batch_size = cfg.github.sync_batch_size
    # Buffer for the current batch: list of (row, needs_analysis) tuples built
    # without holding a DB connection. Flushed every `batch_size` items.
    pending_batch: list[tuple[Repository, bool]] = []

    async def _flush_batch(batch: list[tuple[Repository, bool]]) -> None:
        """Write a batch of new/updated Repository rows in a single transaction."""
        if not batch or dry_run:
            return
        async with db.transaction() as session:
            for row, _needs in batch:
                session.add(row)
            await session.flush()

    async with GitHubAPIClient(token) as client:
        starred_iter = await client.list_starred(since=integration.last_synced_at)
        async for item in starred_iter:
            repo_dto = item.repo
            seen_github_ids.add(repo_dto.id)

            # Look up the existing row outside any long-held transaction.
            async with db.session() as session:
                result = await session.execute(
                    select(Repository).where(
                        Repository.github_id == repo_dto.id,
                        Repository.user_id == integration.user_id,
                    )
                )
                existing = result.scalar_one_or_none()

            if existing is None:
                row = Repository(
                    github_id=repo_dto.id,
                    owner=repo_dto.owner.login,
                    name=repo_dto.name,
                    full_name=repo_dto.full_name,
                    url=repo_dto.html_url,
                    homepage_url=repo_dto.homepage,
                    description=repo_dto.description,
                    primary_language=repo_dto.language,
                    topics_json=list(repo_dto.topics),
                    stars=repo_dto.stargazers_count,
                    forks=repo_dto.forks_count,
                    watchers=repo_dto.watchers_count,
                    default_branch=repo_dto.default_branch,
                    license_spdx=repo_dto.license.spdx_id if repo_dto.license else None,
                    is_archived=repo_dto.archived,
                    is_fork=repo_dto.fork,
                    is_template=repo_dto.is_template,
                    pushed_at=repo_dto.pushed_at,
                    created_at_github=repo_dto.created_at,
                    source=RepoSource.STARRED,
                    is_starred=True,
                    user_id=integration.user_id,
                    # Mark pending so a crash before analysis completes is
                    # resumable: the next sync will re-enqueue this repo.
                    # _persist_analysis() clears this to False on success.
                    pending_analysis=True,
                )
                repos_imported += 1
                needs_analysis = True
                row_for_analysis = row
            else:
                # Update mutable metadata; preserve analysis_json, content_hash,
                # pending_analysis unless we detect content drift below.
                existing.owner = repo_dto.owner.login
                existing.name = repo_dto.name
                existing.full_name = repo_dto.full_name
                existing.url = repo_dto.html_url
                existing.homepage_url = repo_dto.homepage
                existing.description = repo_dto.description
                existing.primary_language = repo_dto.language
                existing.topics_json = list(repo_dto.topics)
                existing.stars = repo_dto.stargazers_count
                existing.forks = repo_dto.forks_count
                existing.watchers = repo_dto.watchers_count
                existing.default_branch = repo_dto.default_branch
                existing.license_spdx = repo_dto.license.spdx_id if repo_dto.license else None
                existing.is_archived = repo_dto.archived
                existing.is_fork = repo_dto.fork
                existing.is_template = repo_dto.is_template
                existing.pushed_at = repo_dto.pushed_at
                existing.created_at_github = repo_dto.created_at
                existing.source = RepoSource.STARRED
                existing.is_starred = True
                repos_updated += 1

                new_hash = _compute_content_hash(existing)
                needs_analysis = (
                    existing.content_hash != new_hash
                    or existing.content_hash is None
                    or existing.pending_analysis
                )
                row_for_analysis = existing

            pending_batch.append((row_for_analysis, needs_analysis))

            # Flush completed batches to DB without holding connections across
            # slow GitHub API pages (avoids pool exhaustion on large star lists).
            if len(pending_batch) >= batch_size:
                await _flush_batch(pending_batch)
                for row, needs in pending_batch:
                    if needs:
                        repos_to_analyze.append(row)
                pending_batch.clear()

        # Flush any remaining items that didn't fill a full batch before
        # running watch checks; a watch-side error must not discard star sync
        # rows already fetched from GitHub.
        await _flush_batch(pending_batch)
        for row, needs in pending_batch:
            if needs:
                repos_to_analyze.append(row)
        pending_batch.clear()

        if not dry_run:
            await _sync_repository_watches(
                client=client,
                db=db,
                user_id=integration.user_id,
                bot=bot,
                correlation_id=correlation_id,
            )

    # Safety net for future edits that append to pending_batch outside the client block.
    await _flush_batch(pending_batch)
    for row, needs in pending_batch:
        if needs:
            repos_to_analyze.append(row)
    pending_batch.clear()

    # Bulk-flip is_starred=False for repos no longer returned by the API.
    # A single UPDATE avoids N per-row transactions and N connection acquisitions.
    repos_unstarred = 0
    if is_full_star_snapshot and seen_github_ids and not dry_run:
        async with db.transaction() as session:
            result = await session.execute(
                update(Repository)
                .where(
                    Repository.user_id == integration.user_id,
                    Repository.github_id.not_in(seen_github_ids),
                    Repository.is_starred == True,  # noqa: E712
                )
                .values(is_starred=False, updated_at=func.now())
                .returning(Repository.id)
            )
            repos_unstarred = len(result.fetchall())
    elif is_full_star_snapshot and seen_github_ids and dry_run:
        # Count what would be unstarred without writing.
        async with db.session() as session:
            result = await session.execute(
                select(func.count()).where(
                    Repository.user_id == integration.user_id,
                    Repository.github_id.not_in(seen_github_ids),
                    Repository.is_starred == True,  # noqa: E712
                )
            )
            repos_unstarred = result.scalar_one() or 0  # type: ignore[assignment]

    # Update integration timestamps
    now = datetime.now(UTC)
    if not dry_run:
        async with db.transaction() as session:
            integ_row = await session.get(UserGitHubIntegration, integration.id)
            if integ_row is not None:
                integ_row.last_synced_at = now
                integ_row.last_sync_cursor = None
                if is_first_sync:
                    integ_row.last_full_sync_at = now

    # Sort oldest-first so budget-cap days favour established repos
    repos_to_analyze.sort(key=lambda r: r.created_at_github or datetime.min.replace(tzinfo=UTC))

    llm_calls_made = [0]
    llm_calls_deferred = [0]
    await _analyze_pending(
        repos_to_analyze,
        settings=cfg,
        db=db,
        correlation_id=correlation_id,
        llm_calls_made=llm_calls_made,
        llm_calls_deferred=llm_calls_deferred,
        dry_run=dry_run,
    )

    return (
        repos_imported,
        repos_updated,
        repos_unstarred,
        llm_calls_made[0],
        llm_calls_deferred[0],
    )


async def _sync_repository_watches(
    *,
    client: GitHubAPIClient,
    db: Database,
    user_id: int,
    bot: Any,
    correlation_id: str,
) -> None:
    """Check watched repositories and emit idempotent README/release events."""
    async with db.session() as session:
        result = await session.execute(
            select(UserRepositoryWatch, Repository)
            .join(Repository, Repository.id == UserRepositoryWatch.repository_id)
            .where(
                UserRepositoryWatch.user_id == user_id,
                Repository.user_id == user_id,
            )
            .order_by(UserRepositoryWatch.id)
        )
        rows = list(result.all())

    for watch, repository in rows:
        if not watch.watch_readme and not watch.watch_releases:
            await _mark_repository_watch_checked(db, watch_id=watch.id)
            continue

        readme_sha256: str | None = None
        release_tag: str | None = None
        release_url: str | None = None
        if watch.watch_readme:
            readme = await client.get_readme(
                repository.owner,
                repository.name,
                ref=repository.default_branch,
            )
            readme_body = readme.content or ""
            readme_sha256 = hashlib.sha256(readme_body.encode()).hexdigest()
        if watch.watch_releases:
            latest_release = await client.get_latest_release(repository.owner, repository.name)
            if latest_release is not None:
                release_tag = latest_release.tag_name
                release_url = latest_release.html_url
            else:
                release_tag = ""

        events = _repository_watch_events_for_state(
            user_id=user_id,
            repository_id=repository.id,
            repository_full_name=repository.full_name,
            repository_url=repository.url,
            release_url=release_url,
            watch_readme=watch.watch_readme,
            watch_releases=watch.watch_releases,
            previous_readme_sha256=watch.last_readme_sha256,
            last_notified_readme_sha256=watch.last_notified_readme_sha256,
            current_readme_sha256=readme_sha256,
            previous_release_tag=watch.last_release_tag,
            last_notified_release_tag=watch.last_notified_release_tag,
            current_release_tag=release_tag,
        )
        for event in events:
            await _emit_repository_watch_triggered(
                event,
                bot=bot,
                correlation_id=correlation_id,
            )

        await _update_repository_watch_state(
            db,
            watch_id=watch.id,
            readme_sha256=readme_sha256 if watch.watch_readme else None,
            notified_readme_sha256=readme_sha256
            if any(event.trigger == "readme" for event in events)
            else None,
            release_tag=release_tag if watch.watch_releases else None,
            notified_release_tag=release_tag
            if any(event.trigger == "release" for event in events)
            else None,
        )


def _repository_watch_events_for_state(
    *,
    user_id: int,
    repository_id: int,
    repository_full_name: str,
    repository_url: str | None,
    release_url: str | None,
    watch_readme: bool,
    watch_releases: bool,
    previous_readme_sha256: str | None,
    last_notified_readme_sha256: str | None,
    current_readme_sha256: str | None,
    previous_release_tag: str | None,
    last_notified_release_tag: str | None,
    current_release_tag: str | None,
) -> list[RepositoryWatchTriggered]:
    events: list[RepositoryWatchTriggered] = []
    if (
        watch_readme
        and current_readme_sha256 is not None
        and previous_readme_sha256 is not None
        and current_readme_sha256 not in {previous_readme_sha256, last_notified_readme_sha256}
    ):
        events.append(
            RepositoryWatchTriggered(
                user_id=user_id,
                repository_id=repository_id,
                repository_full_name=repository_full_name,
                trigger="readme",
                previous_value=previous_readme_sha256,
                current_value=current_readme_sha256,
                url=repository_url,
            )
        )
    if (
        watch_releases
        and current_release_tag is not None
        and current_release_tag != ""
        and previous_release_tag is not None
        and current_release_tag not in {previous_release_tag, last_notified_release_tag}
    ):
        events.append(
            RepositoryWatchTriggered(
                user_id=user_id,
                repository_id=repository_id,
                repository_full_name=repository_full_name,
                trigger="release",
                previous_value=previous_release_tag,
                current_value=current_release_tag,
                url=release_url,
            )
        )
    return events


async def _emit_repository_watch_triggered(
    event: RepositoryWatchTriggered,
    *,
    bot: Any,
    correlation_id: str,
) -> None:
    logger.info(
        "repository_watch_triggered",
        extra={
            "cid": correlation_id,
            "user_id": event.user_id,
            "repository_id": event.repository_id,
            "repository_full_name": event.repository_full_name,
            "trigger": event.trigger,
        },
    )
    if GITHUB_REPOSITORY_WATCH_TRIGGERS_TOTAL is not None:
        GITHUB_REPOSITORY_WATCH_TRIGGERS_TOTAL.labels(trigger=event.trigger).inc()
    if bot is None:
        return
    try:
        if event.trigger == "readme":
            text = (
                f"README changed for {event.repository_full_name}.\n"
                f"Previous: {event.previous_value}\n"
                f"Current: {event.current_value}"
            )
        else:
            text = f"New release for {event.repository_full_name}: {event.current_value}."
        if event.url:
            text = f"{text}\n{event.url}"
        await bot.send_message(chat_id=event.user_id, text=text)
    except Exception:
        logger.exception(
            "repository_watch_telegram_notify_failed",
            extra={
                "cid": correlation_id,
                "user_id": event.user_id,
                "repository_id": event.repository_id,
                "trigger": event.trigger,
            },
        )


async def _mark_repository_watch_checked(db: Database, *, watch_id: int) -> None:
    async with db.transaction() as session:
        watch = await session.get(UserRepositoryWatch, watch_id)
        if watch is not None:
            watch.last_checked_at = datetime.now(UTC)


async def _update_repository_watch_state(
    db: Database,
    *,
    watch_id: int,
    readme_sha256: str | None,
    notified_readme_sha256: str | None,
    release_tag: str | None,
    notified_release_tag: str | None,
) -> None:
    async with db.transaction() as session:
        watch = await session.get(UserRepositoryWatch, watch_id)
        if watch is None:
            return
        if readme_sha256 is not None:
            watch.last_readme_sha256 = readme_sha256
        if notified_readme_sha256 is not None:
            watch.last_notified_readme_sha256 = notified_readme_sha256
        if release_tag is not None:
            watch.last_release_tag = release_tag
        if notified_release_tag is not None:
            watch.last_notified_release_tag = notified_release_tag
        watch.last_checked_at = datetime.now(UTC)


async def _record_github_sync_error(
    db: Database,
    *,
    integration_id: int,
    error: str,
    backoff_until: datetime | None = None,
    rate_limited: bool = False,
) -> int:
    async with db.transaction() as session:
        row = await session.get(UserGitHubIntegration, integration_id)
        if row is None:
            return 0
        failure_count = (
            _github_rate_limit_streak(row) + 1 if rate_limited else _github_failure_count(row) + 1
        )
        row.last_sync_cursor = _github_sync_error_payload(
            error=error,
            failure_count=failure_count,
            backoff_until=backoff_until,
        )
        return failure_count


def _github_failure_count(integration: UserGitHubIntegration) -> int:
    state = _github_sync_state(integration)
    try:
        return int(state.get("failure_count") or 0)
    except (TypeError, ValueError):
        return 0


def _github_rate_limit_streak(integration: UserGitHubIntegration) -> int:
    state = _github_sync_state(integration)
    if not str(state.get("last_error") or "").startswith("rate_limit"):
        return 0
    try:
        return int(state.get("failure_count") or 0)
    except (TypeError, ValueError):
        return 0


def _github_sync_state(integration: UserGitHubIntegration) -> dict[str, Any]:
    raw = getattr(integration, "last_sync_cursor", None)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("kind") != "github_sync_state":
        return {}
    parsed = dict(data)
    backoff_until = parsed.get("backoff_until")
    if isinstance(backoff_until, str):
        try:
            parsed["backoff_until"] = datetime.fromisoformat(backoff_until)
        except ValueError:
            parsed["backoff_until"] = None
    return parsed


def _github_sync_error_payload(
    *,
    error: str,
    failure_count: int,
    backoff_until: datetime | None = None,
) -> str:
    if backoff_until is None:
        backoff_until = datetime.now(UTC) + timedelta(minutes=min(60, 5 * max(1, failure_count)))
    return json.dumps(
        {
            "kind": "github_sync_state",
            "last_error": error[:500],
            "failure_count": failure_count,
            "backoff_until": backoff_until.isoformat(),
        }
    )


async def _analyze_pending(
    repos: list[Repository],
    *,
    settings: AppConfig,
    db: Database,
    correlation_id: str,
    llm_calls_made: list[int],
    llm_calls_deferred: list[int],
    dry_run: bool = False,
) -> None:
    """Run AnalyzeRepositoryUseCase on each repo, subject to concurrency + budget caps."""
    semaphore = asyncio.Semaphore(settings.github.llm_concurrency)
    budget = settings.github.llm_daily_budget

    # Build the analyze use case (LLM client + embedding service + Qdrant store)
    # once per run and reuse it across repos, instead of reconstructing it (a new
    # QdrantClient handshake + fresh embedding model cache) for every repo. Lazy
    # so dry-run / budget-exhausted runs never build it. Safe to share: the
    # builder is synchronous, so the first-caller check cannot interleave.
    _use_case_cache: list[Any] = []

    def _get_use_case() -> Any:
        if not _use_case_cache:
            _use_case_cache.append(_build_analyze_use_case(db, settings))
        return _use_case_cache[0]

    async def _one(repo: Repository) -> None:
        async with semaphore:
            if llm_calls_made[0] >= budget:
                if not dry_run:
                    await _mark_pending(repo.id, db)
                llm_calls_deferred[0] += 1
                return
            llm_calls_made[0] += 1
            if dry_run:
                return
            try:
                use_case = _get_use_case()
                await use_case.analyze(
                    repo.id,
                    correlation_id=correlation_id,
                    chosen_lang="en",
                )
            except Exception:
                logger.exception(
                    "github_sync_analyze_failed",
                    extra={"cid": correlation_id, "repository_id": repo.id},
                )
                # analyze() commits pending_analysis=False inside save_analysis
                # *before* the embedding refresh runs, so a failure after that
                # point (e.g. a transient Qdrant/embedding error) would otherwise
                # leave the row pending_analysis=False with a stored content_hash —
                # orphaning it from every future sync's re-enqueue check. Re-arm
                # pending_analysis so the next run retries this repo.
                if not dry_run:
                    await _mark_pending(repo.id, db)

    # return_exceptions=True so one repo's failure (e.g. a DB error in
    # _mark_pending, which runs in the budget-cap path or the analyze-failure
    # except block) cannot cancel the sibling analyses. Per-repo analyze errors
    # are already logged inside _one.
    await asyncio.gather(*[_one(repo) for repo in repos], return_exceptions=True)


async def _mark_pending(repository_id: int, db: Database) -> None:
    """Set pending_analysis=True on a repository row."""
    async with db.transaction() as session:
        row = await session.get(Repository, repository_id)
        if row is not None:
            row.pending_analysis = True


def _build_analyze_use_case(db: Database, settings: AppConfig) -> Any:
    """Construct AnalyzeRepositoryUseCase with required dependencies."""
    from app.adapters.llm import LLMClientFactory
    from app.agents.repo_analysis_agent import RepoAnalysisAgent
    from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase
    from app.infrastructure.embedding.embedding_factory import create_embedding_service
    from app.infrastructure.embedding.repository_embedding import RepositoryEmbeddingGenerator
    from app.infrastructure.persistence.repositories.repository_analysis_repository import (
        RepositoryAnalysisRepositoryAdapter,
    )

    llm_client = LLMClientFactory.create_from_config(settings)
    embedding_service = create_embedding_service(settings.embedding)

    qdrant_store: object | None = None
    try:
        from app.di.shared import build_qdrant_vector_store

        qdrant_store = build_qdrant_vector_store(settings)
    except Exception:
        qdrant_store = None

    embedding_gen = RepositoryEmbeddingGenerator(
        embedding_service=embedding_service,
        qdrant_store=qdrant_store,  # type: ignore[arg-type]
        db=db,
        environment=settings.vector_store.environment,
        user_scope=settings.vector_store.user_scope,
    )
    agent = RepoAnalysisAgent(llm_service=llm_client)
    repository_repo = RepositoryAnalysisRepositoryAdapter(db)
    return AnalyzeRepositoryUseCase(
        repository_repo=repository_repo,
        agent=agent,
        embedding_gen=embedding_gen,
    )


async def _notify_needs_reauth(
    integration: UserGitHubIntegration,
    bot: Any,
    db: Database,
    correlation_id: str,
) -> None:
    """Send a Telegram DM if the integration hasn't been notified within 7 days."""
    now = datetime.now(UTC)
    seven_days = timedelta(days=7)
    if (
        integration.notified_needs_reauth_at is not None
        and (now - integration.notified_needs_reauth_at) < seven_days
    ):
        return
    if bot is None:
        # No bot was wired: the CLI/dry-run path passes bot=None, or the worker
        # could not build a Telethon client. The scheduled task normally builds
        # a worker-side bot via _sync_body_with_bot, so the real run does deliver
        # the DM; skip cleanly when there is genuinely no client.
        logger.info(
            "needs_reauth_dm_skipped",
            extra={
                "event": "needs_reauth_dm_skipped",
                "reason": "no_bot_available",
                "user_id": integration.user_id,
                "cid": correlation_id,
            },
        )
        return
    try:
        await bot.send_message(
            chat_id=integration.user_id,
            text=(
                "Your GitHub token has been revoked or expired. "
                "Daily starred-repo sync is paused. "
                "Reconnect via the web /repositories settings."
            ),
        )
        async with db.transaction() as session:
            row = await session.get(UserGitHubIntegration, integration.id)
            if row is not None:
                row.notified_needs_reauth_at = now
    except Exception:
        logger.exception(
            "github_sync_dm_failed",
            extra={"cid": correlation_id, "user_id": integration.user_id},
        )
