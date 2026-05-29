"""Persistence adapter for git mirror state.

GitMirrorRepository wraps the ``git_mirrors`` table and provides the query/write
operations that GitMirrorService and the Taskiq job rely on.  All writes go through
``db.transaction()``, reads through ``db.session()``, matching the pattern used
elsewhere in the infrastructure persistence layer.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from sqlalchemy import and_, delete as sql_delete, select

from app.db.models.git_backup import GitMirror, GitMirrorSource, GitMirrorStatus

if TYPE_CHECKING:
    from app.adapters.git_backup.errors import ErrorCategory
    from app.config.git_backup import GitBackupConfig
    from app.db.session import Database


class GitMirrorRepository:
    """SQLAlchemy adapter for ``git_mirrors`` table access."""

    def __init__(self, db: Database, config: GitBackupConfig) -> None:
        self._db = db
        self._config = config

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    async def list_due(self, user_id: int | None = None) -> list[GitMirror]:
        """Return mirrors that are ready to be synced in this run.

        Eligibility rules (matches GitBackupConfig semantics):
        - status is PENDING, OK, or FAILED
        - if auto_skip_failing and consecutive_failures >= max_consecutive_failures:
          skip mirrors whose backoff_until is still in the future
        """
        now = dt.datetime.now(tz=dt.UTC)

        # Mirrors in PENDING / OK are always eligible.
        # Mirrors in FAILED are eligible unless they are in cooldown.
        #   cooldown: auto_skip_failing=True AND consecutive_failures >= threshold
        #             AND backoff_until > now
        cfg = self._config
        in_cooldown = and_(
            GitMirror.status == GitMirrorStatus.FAILED,
            GitMirror.consecutive_failures >= cfg.max_consecutive_failures,
            GitMirror.backoff_until > now,
        )

        base_filter = and_(
            GitMirror.status.in_(
                [GitMirrorStatus.PENDING, GitMirrorStatus.OK, GitMirrorStatus.FAILED]
            ),
            # EXCLUDED rows are tombstoned (permanently gone upstream) and must
            # never be returned for sync.  They can only be revived by a fresh
            # upsert_target call (e.g. the user re-adds the URL via /mirror).
            GitMirror.status != GitMirrorStatus.EXCLUDED,
        )

        eligibility = and_(base_filter, ~in_cooldown) if cfg.auto_skip_failing else base_filter

        stmt = select(GitMirror).where(eligibility).order_by(GitMirror.id)
        if user_id is not None:
            stmt = stmt.where(GitMirror.user_id == user_id)

        async with self._db.session() as session:
            rows = (await session.scalars(stmt)).all()
        return list(rows)

    async def get_by_id(self, mirror_id: int) -> GitMirror | None:
        async with self._db.session() as session:
            return await session.scalar(select(GitMirror).where(GitMirror.id == mirror_id))

    async def list_for_user(self, user_id: int) -> list[GitMirror]:
        async with self._db.session() as session:
            rows = (
                await session.scalars(
                    select(GitMirror).where(GitMirror.user_id == user_id).order_by(GitMirror.id)
                )
            ).all()
        return list(rows)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    async def upsert_target(
        self,
        user_id: int,
        source: GitMirrorSource,
        clone_url: str,
        name: str | None,
        *,
        repository_id: int | None = None,
        size_kb: int | None = None,
    ) -> GitMirror:
        """Create a new mirror row or return the existing one (matched by user+url).

        Existing rows are not mutated so that in-progress state is preserved,
        with these exceptions:
        - EXCLUDED rows are revived to PENDING so the user can retry.
        - ``name`` and ``repository_id`` are updated when a non-None value is
          provided and differs from the stored value.
        - ``size_kb`` is written only when provided (not None) AND the existing
          row has no real post-clone size yet (size_kb IS NULL).  This preserves
          the authoritative on-disk size recorded by record_success while
          allowing the GitHub-reported estimate to be stored on the first upsert.
        """
        async with self._db.transaction() as session:
            existing = await session.scalar(
                select(GitMirror).where(
                    and_(
                        GitMirror.user_id == user_id,
                        GitMirror.clone_url == clone_url,
                    )
                )
            )
            if existing is not None:
                # If the row was tombstoned (EXCLUDED), revive it so the user
                # can retry after re-adding the URL via /mirror or the API.
                if existing.status == GitMirrorStatus.EXCLUDED:
                    existing.status = GitMirrorStatus.PENDING
                    existing.excluded_at = None
                    existing.consecutive_failures = 0
                    existing.backoff_until = None
                    existing.last_error = None
                    existing.last_error_category = None

                # Update linking metadata if provided without resetting run state.
                if name is not None and existing.name != name:
                    existing.name = name
                if repository_id is not None and existing.repository_id != repository_id:
                    existing.repository_id = repository_id
                # Only backfill size_kb when we have a value and the row has not
                # been given an authoritative post-clone measurement yet.
                if size_kb is not None and existing.size_kb is None:
                    existing.size_kb = size_kb
                await session.flush()
                await session.refresh(existing)
                return existing

            row = GitMirror(
                user_id=user_id,
                source=source,
                clone_url=clone_url,
                name=name,
                repository_id=repository_id,
                size_kb=size_kb,
                status=GitMirrorStatus.PENDING,
                consecutive_failures=0,
            )
            session.add(row)
            await session.flush()
            await session.refresh(row)
        return row

    async def record_success(
        self,
        mirror_id: int,
        mirror_path: str,
        size_kb: int | None,
        default_branch: str | None,
        clone_strategy: str | None = None,
    ) -> None:
        """Persist a successful mirror outcome: reset failure counters, record path.

        Also clears ``use_http1_fallback``: a clean sync means the host is fine
        on HTTP/2 again, so the next run should not be burdened with the flag.
        """
        now = dt.datetime.now(tz=dt.UTC)
        async with self._db.transaction() as session:
            row = await session.scalar(select(GitMirror).where(GitMirror.id == mirror_id))
            if row is None:
                return
            row.status = GitMirrorStatus.OK
            row.mirror_path = mirror_path
            row.size_kb = size_kb
            row.default_branch = default_branch
            row.last_mirrored_at = now
            row.last_attempt_at = now
            row.consecutive_failures = 0
            row.backoff_until = None
            row.last_error = None
            row.last_error_category = None
            row.use_http1_fallback = False
            if clone_strategy is not None:
                row.clone_strategy = clone_strategy

    async def record_failure(
        self,
        mirror_id: int,
        error_category: ErrorCategory,
        message: str,
        clone_strategy: str | None = None,
        *,
        use_http1: bool | None = None,
    ) -> None:
        """Persist a failed mirror outcome.

        Increments consecutive_failures; sets backoff_until once the threshold
        from config is exceeded.

        When ``use_http1`` is ``True``, sets ``use_http1_fallback=True`` on the
        row so that the next sync attempt starts with HTTP/1.1.  When ``False``,
        clears the flag.  ``None`` (the default) leaves the column unchanged so
        callers that don't care about the flag never regress existing behaviour.
        """
        now = dt.datetime.now(tz=dt.UTC)
        cfg = self._config
        async with self._db.transaction() as session:
            row = await session.scalar(select(GitMirror).where(GitMirror.id == mirror_id))
            if row is None:
                return
            row.status = GitMirrorStatus.FAILED
            row.last_attempt_at = now
            row.consecutive_failures = (row.consecutive_failures or 0) + 1
            row.last_error = message[:4000] if message else None  # guard column width
            row.last_error_category = error_category.value
            if clone_strategy is not None:
                row.clone_strategy = clone_strategy
            if use_http1 is not None:
                row.use_http1_fallback = use_http1

            if (
                cfg.auto_skip_failing
                and row.consecutive_failures >= cfg.max_consecutive_failures
                and cfg.failure_cooldown_hours > 0
            ):
                row.backoff_until = now + dt.timedelta(hours=cfg.failure_cooldown_hours)

    async def record_skip(self, mirror_id: int, reason: str) -> None:
        """Mark a mirror as skipped for this run (does not change failure counters)."""
        now = dt.datetime.now(tz=dt.UTC)
        async with self._db.transaction() as session:
            row = await session.scalar(select(GitMirror).where(GitMirror.id == mirror_id))
            if row is None:
                return
            row.status = GitMirrorStatus.SKIPPED
            row.last_attempt_at = now
            row.last_error = reason[:4000] if reason else None

    async def list_stale_excluded(self, older_than_days: int) -> list[GitMirror]:
        """Return EXCLUDED mirrors whose excluded_at is older than ``older_than_days``.

        Only rows where excluded_at IS NOT NULL are considered.  Mirrors that
        were excluded before the cutoff are candidates for the prune sweep.
        """
        cutoff = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=older_than_days)
        stmt = select(GitMirror).where(
            and_(
                GitMirror.status == GitMirrorStatus.EXCLUDED,
                GitMirror.excluded_at.is_not(None),
                GitMirror.excluded_at < cutoff,
            )
        )
        async with self._db.session() as session:
            rows = (await session.scalars(stmt)).all()
        return list(rows)

    async def delete_mirror(self, mirror_id: int) -> None:
        """Hard-delete a ``git_mirrors`` row by primary key.

        Used by the stale-EXCLUDED prune sweep.  Silently does nothing if the
        row no longer exists (concurrent deletion is not an error).
        """
        async with self._db.transaction() as session:
            await session.execute(sql_delete(GitMirror).where(GitMirror.id == mirror_id))

    async def record_excluded(self, mirror_id: int, reason: str) -> None:
        """Tombstone a mirror whose upstream repository is permanently gone.

        Sets status=EXCLUDED and excluded_at=now so the mirror is never
        returned by list_due again.  The row can be revived by a fresh
        upsert_target call (e.g. the user re-adds the URL via /mirror or the
        API), which resets status to PENDING and clears excluded_at.
        """
        now = dt.datetime.now(tz=dt.UTC)
        async with self._db.transaction() as session:
            row = await session.scalar(select(GitMirror).where(GitMirror.id == mirror_id))
            if row is None:
                return
            row.status = GitMirrorStatus.EXCLUDED
            row.excluded_at = now
            row.last_attempt_at = now
            row.last_error = reason[:4000] if reason else None
