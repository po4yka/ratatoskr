"""Tests for dry-run plan-line output (feature A) and total_failures / last_failure_at
write path (feature B).

Feature A — perform_sync(dry_run=True) plan lines:
- Emits exactly one INFO log line per collected task with the format
  ``git_mirror_dry_run_plan name=<name> dest=<dir> argv=<redacted argv>``.
- The logged argv must NOT contain any embedded credential (raw token redacted).
- No git subprocess is executed (git_runner called zero times).
- The aggregate count line is still emitted and the returned SyncSummary is
  correct (ok == number of tasks).

Feature B — record_failure increments total_failures / sets last_failure_at:
- record_failure increments total_failures by 1 (starting from 0 or any prior value).
- record_failure sets last_failure_at to the current UTC timestamp.
- record_success does NOT reset total_failures (it is a lifetime counter).
- record_success does NOT change last_failure_at.

All tests are hermetic: no real DB, no filesystem, no subprocess calls.
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.git_backup.errors import ErrorCategory
from app.adapters.git_backup.mirror_service import GitMirrorService
from app.config.git_backup import GitBackupConfig
from app.db.models.git_backup import GitMirror, GitMirrorSource, GitMirrorStatus

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_URL_NO_CREDS = "https://github.com/octocat/hello-world.git"
_URL_WITH_CREDS = "https://x-access-token:ghp_super_secret_token@github.com/octocat/hello-world.git"
_NAME = "octocat/hello-world"


def _make_config(**overrides: Any) -> GitBackupConfig:
    base: dict[str, Any] = {
        "GIT_BACKUP_ENABLED": False,
        "GIT_BACKUP_DATA_PATH": "/tmp/git-dryrun-test",
    }
    base.update(overrides)
    return GitBackupConfig.model_validate(base)


def _make_mirror(
    *,
    mirror_id: int = 1,
    status: GitMirrorStatus = GitMirrorStatus.PENDING,
    consecutive_failures: int = 0,
    total_failures: int = 0,
    use_http1_fallback: bool = False,
    last_failure_at: dt.datetime | None = None,
) -> GitMirror:
    m = GitMirror(
        id=mirror_id,
        user_id=100,
        source=GitMirrorSource.GITHUB,
        clone_url=_URL_NO_CREDS,
        name=_NAME,
        consecutive_failures=consecutive_failures,
        status=status,
    )
    m.total_failures = total_failures
    m.use_http1_fallback = use_http1_fallback
    m.last_failure_at = last_failure_at
    return m


# ---------------------------------------------------------------------------
# Fake repo for service-level dry-run tests
# ---------------------------------------------------------------------------


class _FakeMirrorRepo:
    """Injectable fake for GitMirrorRepository that records call counts."""

    def __init__(self, mirrors: list[GitMirror]) -> None:
        self._mirrors = mirrors
        self.git_runner_call_count = 0

    async def list_due(self, user_id: int | None = None) -> list[GitMirror]:
        return list(self._mirrors)

    async def record_success(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def record_failure(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def record_excluded(self, mirror_id: int, reason: str) -> None:
        pass

    async def record_skip(self, mirror_id: int, reason: str) -> None:
        pass


def _make_service(
    fake_repo: _FakeMirrorRepo,
    git_runner: Any,
    *,
    data_path: str = "/tmp/git-dryrun-test",
) -> GitMirrorService:
    cfg = _make_config(GIT_BACKUP_DATA_PATH=data_path)
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    from app.adapters.git_backup.circuit_breaker import StorageCircuitBreaker
    from app.adapters.git_backup.retry import RetryPolicy

    return GitMirrorService(
        config=cfg,
        mirror_repo=fake_repo,  # type: ignore[arg-type]
        db=db,
        retry_policy=RetryPolicy(max_attempts=1, base_delay_ms=0),
        circuit_breaker=StorageCircuitBreaker(threshold=100),
        maintenance=None,
        lfs=None,
        git_runner=git_runner,
    )


# ---------------------------------------------------------------------------
# Feature A — dry-run plan-line tests
# ---------------------------------------------------------------------------


class TestDryRunPlanLines:
    """perform_sync(dry_run=True) must emit per-task plan lines and never run git."""

    @pytest.mark.asyncio
    async def test_dry_run_emits_one_plan_line_per_task(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Each collected task must produce exactly one git_mirror_dry_run_plan log line."""
        mirrors = [
            _make_mirror(mirror_id=1),
            _make_mirror(mirror_id=2),
            _make_mirror(mirror_id=3),
        ]
        fake_repo = _FakeMirrorRepo(mirrors)
        git_runner_calls: list[Any] = []

        async def counting_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            git_runner_calls.append(argv)
            return 0, ""

        service = _make_service(fake_repo, counting_runner)

        with (
            patch("pathlib.Path.exists", return_value=False),
            patch.object(service, "_resolve_url", return_value=(_URL_NO_CREDS, None)),
            caplog.at_level(logging.INFO, logger="app.adapters.git_backup.mirror_service"),
        ):
            summary = await service.perform_sync(user_id=100, dry_run=True)

        plan_lines = [r for r in caplog.records if "git_mirror_dry_run_plan" in r.message]
        assert len(plan_lines) == 3, (
            f"Expected 3 plan lines, got {len(plan_lines)}: {[r.message for r in plan_lines]}"
        )
        assert git_runner_calls == [], "git_runner must not be called during dry-run"
        assert summary.ok == 3

    @pytest.mark.asyncio
    async def test_dry_run_plan_line_contains_name_and_dest(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Plan line must contain the mirror name and destination directory."""
        mirror = _make_mirror(mirror_id=7)
        fake_repo = _FakeMirrorRepo([mirror])

        async def noop_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            return 0, ""

        service = _make_service(fake_repo, noop_runner)

        with (
            patch("pathlib.Path.exists", return_value=False),
            patch.object(service, "_resolve_url", return_value=(_URL_NO_CREDS, None)),
            caplog.at_level(logging.INFO, logger="app.adapters.git_backup.mirror_service"),
        ):
            await service.perform_sync(user_id=100, dry_run=True)

        plan_lines = [r for r in caplog.records if "git_mirror_dry_run_plan" in r.message]
        assert len(plan_lines) == 1
        msg = plan_lines[0].message
        assert "name=" in msg
        assert "dest=" in msg
        assert "argv=" in msg

    @pytest.mark.asyncio
    async def test_dry_run_plan_line_redacts_credentials(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Credentials embedded in the effective_url must not appear in the plan log line."""
        mirror = _make_mirror(mirror_id=11)
        fake_repo = _FakeMirrorRepo([mirror])

        async def noop_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            return 0, ""

        service = _make_service(fake_repo, noop_runner)

        with (
            patch("pathlib.Path.exists", return_value=False),
            # Simulate an effective_url that already has credentials injected
            patch.object(service, "_resolve_url", return_value=(_URL_WITH_CREDS, None)),
            caplog.at_level(logging.INFO, logger="app.adapters.git_backup.mirror_service"),
        ):
            await service.perform_sync(user_id=100, dry_run=True)

        plan_lines = [r for r in caplog.records if "git_mirror_dry_run_plan" in r.message]
        assert len(plan_lines) == 1
        msg = plan_lines[0].message
        # The raw token must not appear in the log.
        assert "ghp_super_secret_token" not in msg
        # The credential segment must be redacted.
        assert "***@" in msg or "x-access-token" not in msg

    @pytest.mark.asyncio
    async def test_dry_run_git_runner_never_called(self) -> None:
        """git_runner must receive zero calls when dry_run=True."""
        mirror = _make_mirror(mirror_id=5)
        fake_repo = _FakeMirrorRepo([mirror])
        call_count = 0

        async def strict_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            nonlocal call_count
            call_count += 1
            raise AssertionError("git_runner must not be called during dry-run")

        service = _make_service(fake_repo, strict_runner)

        with (
            patch("pathlib.Path.exists", return_value=False),
            patch.object(service, "_resolve_url", return_value=(_URL_NO_CREDS, None)),
        ):
            summary = await service.perform_sync(user_id=100, dry_run=True)

        assert call_count == 0
        assert summary.ok == 1

    @pytest.mark.asyncio
    async def test_dry_run_aggregate_count_line_emitted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The aggregate 'N tasks would run' line must still be emitted."""
        mirrors = [_make_mirror(mirror_id=1), _make_mirror(mirror_id=2)]
        fake_repo = _FakeMirrorRepo(mirrors)

        async def noop_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            return 0, ""

        service = _make_service(fake_repo, noop_runner)

        with (
            patch("pathlib.Path.exists", return_value=False),
            patch.object(service, "_resolve_url", return_value=(_URL_NO_CREDS, None)),
            caplog.at_level(logging.INFO, logger="app.adapters.git_backup.mirror_service"),
        ):
            await service.perform_sync(user_id=100, dry_run=True)

        aggregate_lines = [
            r
            for r in caplog.records
            if "git_mirror_dry_run" in r.message and "tasks would run" in r.message
        ]
        assert len(aggregate_lines) == 1
        assert "2" in aggregate_lines[0].message

    @pytest.mark.asyncio
    async def test_dry_run_returns_synthetic_sync_summary(self) -> None:
        """SyncSummary.ok must equal the task count; failed and skipped must be 0."""
        mirrors = [_make_mirror(mirror_id=i) for i in range(4)]
        fake_repo = _FakeMirrorRepo(mirrors)

        async def noop_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            return 0, ""

        service = _make_service(fake_repo, noop_runner)

        with (
            patch("pathlib.Path.exists", return_value=False),
            patch.object(service, "_resolve_url", return_value=(_URL_NO_CREDS, None)),
        ):
            summary = await service.perform_sync(user_id=100, dry_run=True)

        assert summary.ok == 4
        assert summary.failed == 0
        assert summary.skipped == 0
        assert len(summary.outcomes) == 4
        for o in summary.outcomes:
            assert o.ok is True

    @pytest.mark.asyncio
    async def test_dry_run_plan_argv_contains_git_operation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The plan argv must include a git clone or remote update operation token."""
        mirror = _make_mirror(mirror_id=20)
        fake_repo = _FakeMirrorRepo([mirror])

        async def noop_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            return 0, ""

        service = _make_service(fake_repo, noop_runner)

        with (
            patch("pathlib.Path.exists", return_value=False),
            patch.object(service, "_resolve_url", return_value=(_URL_NO_CREDS, None)),
            caplog.at_level(logging.INFO, logger="app.adapters.git_backup.mirror_service"),
        ):
            await service.perform_sync(user_id=100, dry_run=True)

        plan_lines = [r for r in caplog.records if "git_mirror_dry_run_plan" in r.message]
        assert len(plan_lines) == 1
        msg = plan_lines[0].message
        # For a new (non-existing) dest, the argv must include a clone-type operation.
        assert any(op in msg for op in ("clone", "remote", "fetch")), (
            f"Plan argv must contain a git operation token; got: {msg}"
        )

    @pytest.mark.asyncio
    async def test_dry_run_no_tasks_no_plan_lines(self, caplog: pytest.LogCaptureFixture) -> None:
        """When there are no due tasks, zero plan lines should be emitted."""
        fake_repo = _FakeMirrorRepo([])

        async def noop_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            return 0, ""

        service = _make_service(fake_repo, noop_runner)

        with (
            patch("pathlib.Path.exists", return_value=False),
            patch.object(service, "_resolve_url", return_value=(_URL_NO_CREDS, None)),
            caplog.at_level(logging.INFO, logger="app.adapters.git_backup.mirror_service"),
        ):
            summary = await service.perform_sync(user_id=100, dry_run=True)

        plan_lines = [r for r in caplog.records if "git_mirror_dry_run_plan" in r.message]
        assert len(plan_lines) == 0
        assert summary.ok == 0


# ---------------------------------------------------------------------------
# Fake DB helpers for repository-level tests (matching http1_fallback style)
# ---------------------------------------------------------------------------


class _FakeTransactionSession:
    """Minimal session fake for record_success / record_failure mutations."""

    def __init__(self, row: GitMirror | None) -> None:
        self._row = row

    async def scalar(self, stmt: Any) -> GitMirror | None:
        return self._row

    async def flush(self) -> None:
        pass

    async def refresh(self, obj: GitMirror) -> None:
        pass

    def add(self, obj: GitMirror) -> None:
        pass


class _FakeTransactionCtx:
    def __init__(self, row: GitMirror | None) -> None:
        self._session = _FakeTransactionSession(row)

    async def __aenter__(self) -> _FakeTransactionSession:
        return self._session

    async def __aexit__(self, *args: Any) -> None:
        pass


class _FakeDB:
    def __init__(self, row: GitMirror | None) -> None:
        self._ctx = _FakeTransactionCtx(row)

    def transaction(self) -> _FakeTransactionCtx:
        return self._ctx


# ---------------------------------------------------------------------------
# Feature B — total_failures / last_failure_at write tests
# ---------------------------------------------------------------------------


class TestRecordFailureTotalFailures:
    """record_failure must increment total_failures and set last_failure_at."""

    @pytest.mark.asyncio
    async def test_record_failure_increments_total_failures_from_zero(self) -> None:
        """First failure: total_failures goes from 0 to 1."""
        from app.adapters.git_backup.repository import GitMirrorRepository

        row = _make_mirror(mirror_id=1, total_failures=0)
        db = _FakeDB(row)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        await repo.record_failure(
            mirror_id=1,
            user_id=100,
            error_category=ErrorCategory.NETWORK_ERROR,
            message="connection reset",
        )

        assert row.total_failures == 1, (
            f"Expected total_failures=1 after first failure, got {row.total_failures}"
        )

    @pytest.mark.asyncio
    async def test_record_failure_increments_total_failures_accumulates(self) -> None:
        """total_failures accumulates across multiple calls (5 → 6)."""
        from app.adapters.git_backup.repository import GitMirrorRepository

        row = _make_mirror(mirror_id=2, total_failures=5)
        db = _FakeDB(row)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        await repo.record_failure(
            mirror_id=2,
            user_id=100,
            error_category=ErrorCategory.TIMEOUT,
            message="timed out",
        )

        assert row.total_failures == 6, (
            f"Expected total_failures=6 after incrementing from 5, got {row.total_failures}"
        )

    @pytest.mark.asyncio
    async def test_record_failure_sets_last_failure_at(self) -> None:
        """record_failure must set last_failure_at to a recent UTC timestamp."""
        from app.adapters.git_backup.repository import GitMirrorRepository

        row = _make_mirror(mirror_id=3, last_failure_at=None)
        db = _FakeDB(row)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        before = dt.datetime.now(tz=dt.UTC)
        await repo.record_failure(
            mirror_id=3,
            user_id=100,
            error_category=ErrorCategory.AUTH_ERROR,
            message="auth failed",
        )
        after = dt.datetime.now(tz=dt.UTC)

        assert row.last_failure_at is not None, "last_failure_at must be set after record_failure"
        assert before <= row.last_failure_at <= after, (
            f"last_failure_at={row.last_failure_at!r} is not in the expected range "
            f"[{before!r}, {after!r}]"
        )

    @pytest.mark.asyncio
    async def test_record_failure_updates_last_failure_at_on_repeated_calls(self) -> None:
        """Subsequent failures update last_failure_at to the new timestamp."""
        from app.adapters.git_backup.repository import GitMirrorRepository

        old_ts = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
        row = _make_mirror(mirror_id=4, last_failure_at=old_ts)
        db = _FakeDB(row)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        before = dt.datetime.now(tz=dt.UTC)
        await repo.record_failure(
            mirror_id=4,
            user_id=100,
            error_category=ErrorCategory.UNKNOWN,
            message="unknown error",
        )
        after = dt.datetime.now(tz=dt.UTC)

        assert row.last_failure_at is not None
        assert row.last_failure_at > old_ts, (
            "last_failure_at must be updated to a more recent value on repeated failures"
        )
        assert before <= row.last_failure_at <= after

    @pytest.mark.asyncio
    async def test_record_failure_also_increments_consecutive_failures(self) -> None:
        """Existing consecutive_failures behaviour must be preserved alongside the new fields."""
        from app.adapters.git_backup.repository import GitMirrorRepository

        row = _make_mirror(mirror_id=5, consecutive_failures=2, total_failures=2)
        db = _FakeDB(row)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        await repo.record_failure(
            mirror_id=5,
            user_id=100,
            error_category=ErrorCategory.SSL_ERROR,
            message="ssl error",
        )

        assert row.consecutive_failures == 3
        assert row.total_failures == 3


class TestRecordSuccessDoesNotResetTotalFailures:
    """record_success must NOT touch total_failures or last_failure_at."""

    @pytest.mark.asyncio
    async def test_record_success_leaves_total_failures_unchanged(self) -> None:
        """total_failures is a lifetime counter; a success must not reset it."""
        from app.adapters.git_backup.repository import GitMirrorRepository

        row = _make_mirror(mirror_id=10, total_failures=7)
        db = _FakeDB(row)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        await repo.record_success(
            mirror_id=10,
            user_id=100,
            mirror_path="/data/test.git",
            size_kb=1024,
            default_branch="main",
        )

        assert row.total_failures == 7, (
            f"record_success must not reset total_failures; expected 7, got {row.total_failures}"
        )

    @pytest.mark.asyncio
    async def test_record_success_leaves_last_failure_at_unchanged(self) -> None:
        """record_success must not clear last_failure_at (only failures set it)."""
        from app.adapters.git_backup.repository import GitMirrorRepository

        ts = dt.datetime(2025, 6, 15, 12, 0, 0, tzinfo=dt.UTC)
        row = _make_mirror(mirror_id=11, last_failure_at=ts)
        db = _FakeDB(row)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        await repo.record_success(
            mirror_id=11,
            user_id=100,
            mirror_path="/data/test.git",
            size_kb=512,
            default_branch="main",
        )

        assert row.last_failure_at == ts, (
            "record_success must not modify last_failure_at; "
            f"expected {ts!r}, got {row.last_failure_at!r}"
        )

    @pytest.mark.asyncio
    async def test_record_success_still_resets_consecutive_failures(self) -> None:
        """Verify that the consecutive_failures reset still works (regression guard)."""
        from app.adapters.git_backup.repository import GitMirrorRepository

        row = _make_mirror(mirror_id=12, consecutive_failures=4, total_failures=10)
        db = _FakeDB(row)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        await repo.record_success(
            mirror_id=12,
            user_id=100,
            mirror_path="/data/test.git",
            size_kb=256,
            default_branch="main",
        )

        # consecutive_failures must be reset; total_failures must not.
        assert row.consecutive_failures == 0
        assert row.total_failures == 10
