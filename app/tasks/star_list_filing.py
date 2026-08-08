"""Taskiq task: file starred repositories that carry no star list.

The nightly star sync mirrors GitHub into the database in one direction only, so a
repository starred anywhere other than this deployment -- the GitHub web UI, a
phone, somebody's script -- lands with an empty ``list_names`` and stays unfiled
forever. This pass closes the loop: it asks the same suggester the add-repository
flow uses and writes the answer back to GitHub.

Four properties are load-bearing, and the tests pin each of them:

1. A repository that already carries a list is never touched. GitHub's
   ``updateUserListsForItem`` takes the complete desired set, so re-filing an
   already-filed repository is an opportunity to silently move it somewhere else.
   The candidate query excludes them, and the write path re-checks under the same
   transaction the suggestion was built from.
2. An inconclusive suggestion writes nothing. Leaving a repository unfiled is the
   documented outcome of the suggester; a wrong list is silent and the 32 slots
   are scarce, so guessing is worse than abstaining.
3. One repository's failure does not end the run. A missing OAuth scope or a
   rate limit on repo N must not strand the rest of the batch.
4. The batch limit is a cost ceiling, not a correctness knob. Every candidate
   costs a vector search and, when the neighbours disagree, one structured LLM
   call. The backlog drains over successive nights instead of in one bill.

This job needs the ``user`` OAuth scope that list writes require. A deployment
whose token predates that scope keeps working for everything else and simply logs
the refusal here, which is why the whole pass is opt-in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, or_, select
from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 -- taskiq resolves type hints at runtime
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.db.models.repository import (
    GitHubIntegrationStatus,
    Repository,
    UserGitHubIntegration,
)
from app.db.session import Database  # noqa: TC001 -- taskiq resolves type hints at runtime
from app.infrastructure.locks.redis_lock import RedisDistributedLock
from app.infrastructure.redis import get_redis
from app.observability.metrics_repositories import (
    GITHUB_FILING_BACKLOG,
    GITHUB_FILING_REPOS_FILED_TOTAL,
    GITHUB_FILING_REPOS_UNRESOLVED_TOTAL,
)
from app.tasks.broker import broker
from app.tasks.deps import get_app_config, get_db

if TYPE_CHECKING:
    from app.core.star_list_suggestion_schema import StarListCandidate

logger = get_logger(__name__)

_LOCK_KEY = "task_lock:github_star_list_filing"
# Worst case is batch_limit repositories each paying for one LLM call; the
# default batch of 25 at ~18 s/call sits well inside this.
_LOCK_TTL = 1800


@dataclass(frozen=True)
class FilingSummary:
    """What the pass actually did, including the parts it declined to do."""

    users_processed: int = 0
    candidates_seen: int = 0
    repos_filed: int = 0
    repos_unresolved: int = 0
    backlog_remaining: int = 0
    errors_per_user: dict[int, str] = field(default_factory=dict)


def _unfiled_predicate() -> Any:
    """Match rows whose ``list_names`` is absent or an empty JSONB array.

    ``list_names`` is nullable *and* defaults to ``[]``, so both spellings of
    "no list" exist in the table and a plain ``IS NULL`` would miss most of them.
    """
    return or_(
        Repository.list_names.is_(None),
        func.coalesce(func.jsonb_array_length(Repository.list_names), 0) == 0,
    )


@broker.task(
    task_name="ratatoskr.github.file_unfiled",
    retry_on_error=True,
    max_retries=2,
)
async def file_unfiled_repositories(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> FilingSummary:
    """File every active integration's unfiled starred repositories."""
    return await run_filing_pass(cfg, db)


async def run_filing_pass(cfg: AppConfig, db: Database) -> FilingSummary:
    """The task body, minus the broker decorator so it can be called directly.

    The feature gate and the lock live here rather than in the decorated task so
    that both are exercised by tests; a stubbed taskiq turns the decorated symbol
    into a mock that cannot be awaited.
    """
    if not cfg.github.star_list_filing_enabled:
        logger.info("star_list_filing_disabled")
        return FilingSummary()

    redis_client = await get_redis(cfg)
    async with RedisDistributedLock(redis_client, _LOCK_KEY, _LOCK_TTL) as acquired:
        if not acquired:
            logger.info("star_list_filing_skipped_lock_held", extra={"key": _LOCK_KEY})
            return FilingSummary()
        return await _file_all_integrations(cfg, db)


async def _file_all_integrations(cfg: AppConfig, db: Database) -> FilingSummary:
    from app.di.tasks import build_star_list_filing_task_runtime

    async with db.transaction() as session:
        result = await session.execute(
            select(UserGitHubIntegration.user_id).where(
                UserGitHubIntegration.status == GitHubIntegrationStatus.ACTIVE
            )
        )
        user_ids: list[int] = [row[0] for row in result.all()]

    if not user_ids:
        logger.info("star_list_filing_no_active_integrations")
        return FilingSummary()

    runtime = build_star_list_filing_task_runtime(cfg, db)
    if runtime.suggester is None:
        # No vector store means no neighbours to vote with. The LLM fallback alone
        # would classify every repository from scratch against 32 names, which is
        # both the expensive path and the one the design treats as a fallback.
        logger.warning("star_list_filing_no_suggester")
        return FilingSummary()

    users_processed = 0
    candidates_seen = 0
    repos_filed = 0
    repos_unresolved = 0
    backlog_remaining = 0
    errors: dict[int, str] = {}

    for user_id in user_ids:
        try:
            outcome = await _file_for_user(cfg=cfg, db=db, runtime=runtime, user_id=user_id)
        except Exception as exc:
            raise_if_cancelled(exc)
            # One user's broken token must not strand the others. Single-tenant
            # today, but the loop is the same shape as the star sync's.
            logger.warning(
                "star_list_filing_user_failed",
                extra={"user_id": user_id, "error": str(exc)},
            )
            errors[user_id] = str(exc)
            continue
        users_processed += 1
        candidates_seen += outcome.candidates_seen
        repos_filed += outcome.repos_filed
        repos_unresolved += outcome.repos_unresolved
        backlog_remaining += outcome.backlog_remaining

    if GITHUB_FILING_BACKLOG is not None:
        GITHUB_FILING_BACKLOG.set(backlog_remaining)

    logger.info(
        "star_list_filing_completed",
        extra={
            "users_processed": users_processed,
            "candidates_seen": candidates_seen,
            "repos_filed": repos_filed,
            "repos_unresolved": repos_unresolved,
            "backlog_remaining": backlog_remaining,
        },
    )
    return FilingSummary(
        users_processed=users_processed,
        candidates_seen=candidates_seen,
        repos_filed=repos_filed,
        repos_unresolved=repos_unresolved,
        backlog_remaining=backlog_remaining,
        errors_per_user=errors,
    )


@dataclass(frozen=True)
class _UserOutcome:
    candidates_seen: int
    repos_filed: int
    repos_unresolved: int
    backlog_remaining: int


async def _file_for_user(
    *,
    cfg: AppConfig,
    db: Database,
    runtime: Any,
    user_id: int,
) -> _UserOutcome:
    """File one user's batch. Raises only on failures that abort the whole user."""
    github_cfg = cfg.github
    cutoff = datetime.now(UTC) - timedelta(hours=github_cfg.star_list_filing_min_age_hours)

    async with db.transaction() as session:
        backlog = await session.scalar(
            select(func.count())
            .select_from(Repository)
            .where(
                Repository.user_id == user_id,
                Repository.is_starred.is_(True),
                _unfiled_predicate(),
            )
        )
        result = await session.execute(
            select(
                Repository.id,
                Repository.full_name,
                Repository.description,
                Repository.primary_language,
                Repository.topics_json,
                Repository.readme_excerpt,
            )
            .where(
                Repository.user_id == user_id,
                Repository.is_starred.is_(True),
                Repository.created_at < cutoff,
                _unfiled_predicate(),
            )
            # Oldest first: a repository that has waited longest is the one the
            # user is least likely to still be filing by hand.
            .order_by(Repository.created_at.asc())
            .limit(github_cfg.star_list_filing_batch_limit)
        )
        candidates = list(result.all())

    if not candidates:
        return _UserOutcome(0, 0, 0, int(backlog or 0))

    # Read the live lists once per user. Names are what both the suggester and
    # the write path speak, and a list renamed on GitHub since the last sync
    # would otherwise be offered as a candidate that cannot be written.
    live = await runtime.star_lists.list_lists(user_id)
    candidate_lists = _to_candidates(live)
    if not candidate_lists:
        logger.info("star_list_filing_no_lists", extra={"user_id": user_id})
        return _UserOutcome(len(candidates), 0, len(candidates), int(backlog or 0))

    filed = 0
    unresolved = 0
    for row in candidates:
        try:
            applied = await _file_one(
                runtime=runtime,
                user_id=user_id,
                row=row,
                candidate_lists=candidate_lists,
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "star_list_filing_repo_failed",
                extra={"user_id": user_id, "full_name": row.full_name, "error": str(exc)},
            )
            unresolved += 1
            _count_unresolved("error")
            continue
        if applied:
            filed += 1
        else:
            unresolved += 1

    return _UserOutcome(len(candidates), filed, unresolved, int(backlog or 0))


async def _file_one(
    *,
    runtime: Any,
    user_id: int,
    row: Any,
    candidate_lists: list[StarListCandidate],
) -> bool:
    """Suggest and apply for a single repository. Returns whether it was filed."""
    suggestion = await runtime.suggester.suggest(
        user_id=user_id,
        repository_id=row.id,
        full_name=row.full_name,
        description=row.description,
        language=row.primary_language,
        topics=list(row.topics_json or []),
        readme_excerpt=row.readme_excerpt,
        available_lists=candidate_lists,
        correlation_id=None,
    )
    if not suggestion.list_names:
        logger.info(
            "star_list_filing_inconclusive",
            extra={"user_id": user_id, "full_name": row.full_name},
        )
        _count_unresolved("inconclusive")
        return False

    applied = await runtime.star_lists.set_repository_lists(
        repository_id=row.id,
        user_id=user_id,
        list_names=list(suggestion.list_names),
    )
    logger.info(
        "star_list_filing_applied",
        extra={
            "user_id": user_id,
            "full_name": row.full_name,
            "lists": list(applied),
            "source": suggestion.source,
            "confidence": round(float(suggestion.confidence), 3),
        },
    )
    if GITHUB_FILING_REPOS_FILED_TOTAL is not None:
        GITHUB_FILING_REPOS_FILED_TOTAL.labels(source=suggestion.source).inc()
    return True


def _count_unresolved(reason: str) -> None:
    if GITHUB_FILING_REPOS_UNRESOLVED_TOTAL is not None:
        GITHUB_FILING_REPOS_UNRESOLVED_TOTAL.labels(reason=reason).inc()


def _to_candidates(live: list[Any]) -> list[StarListCandidate]:
    from app.core.star_list_suggestion_schema import StarListCandidate

    return [
        StarListCandidate(name=entry.name, description=entry.description or "")
        for entry in live
        if getattr(entry, "name", "")
    ]
