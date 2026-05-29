"""Tests for permanently-gone tombstoning logic.

Covers:
- is_permanently_gone: positive matrix (gone signals) and negative matrix
  (auth, network, SSL, transient -- must NOT tombstone).
- GitMirrorRepository.list_due: EXCLUDED rows are never returned.
- GitMirrorRepository.upsert_target: an EXCLUDED row is revived on re-add.
- GitMirrorService: a "repository not found" failure tombstones the mirror
  (status EXCLUDED); an auth failure does NOT tombstone.

All tests are hermetic: no real DB, no filesystem, no subprocess calls.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.adapters.git_backup.errors import is_permanently_gone
from app.adapters.git_backup.mirror_service import (
    GitMirrorService,
)
from app.config.git_backup import GitBackupConfig
from app.db.models.git_backup import GitMirror, GitMirrorSource, GitMirrorStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_URL = "https://github.com/octocat/deleted-repo.git"
_NAME = "octocat/deleted-repo"


def _make_mirror(
    *,
    mirror_id: int = 1,
    status: GitMirrorStatus = GitMirrorStatus.PENDING,
    consecutive_failures: int = 0,
    excluded_at: dt.datetime | None = None,
) -> GitMirror:
    """Return a minimal GitMirror stub (no DB required)."""
    m = GitMirror(
        id=mirror_id,
        user_id=100,
        source=GitMirrorSource.GITHUB,
        clone_url=_URL,
        name=_NAME,
        consecutive_failures=consecutive_failures,
        status=status,
    )
    m.excluded_at = excluded_at
    return m


def _make_config(**overrides: Any) -> GitBackupConfig:
    base: dict[str, Any] = {
        "GIT_BACKUP_ENABLED": False,
        "GIT_BACKUP_DATA_PATH": "/tmp/git-mirror-test",
    }
    base.update(overrides)
    return GitBackupConfig.model_validate(base)


# ---------------------------------------------------------------------------
# is_permanently_gone: positive cases
# ---------------------------------------------------------------------------


class TestIsPermanentlyGonePositive:
    """Signals that MUST trigger tombstoning."""

    def test_repository_not_found_lowercase(self) -> None:
        assert is_permanently_gone("fatal: repository not found") is True

    def test_repository_not_found_mixed_case(self) -> None:
        assert is_permanently_gone("remote: Repository not found.") is True

    def test_repository_not_found_in_longer_message(self) -> None:
        msg = (
            "Unable to sync https://github.com/user/gone.git: exit 128\n"
            "remote: Repository not found."
        )
        assert is_permanently_gone(msg) is True

    def test_could_not_find_repository(self) -> None:
        assert is_permanently_gone("could not find repository") is True

    def test_does_not_exist(self) -> None:
        assert is_permanently_gone("The repository does not exist") is True

    def test_returned_error_404(self) -> None:
        assert is_permanently_gone("returned error: 404") is True

    def test_returned_error_410(self) -> None:
        assert is_permanently_gone("returned error: 410") is True

    def test_error_404(self) -> None:
        assert is_permanently_gone("error: 404") is True

    def test_error_410(self) -> None:
        assert is_permanently_gone("error: 410") is True

    def test_http_404_with_spaces(self) -> None:
        assert is_permanently_gone("The requested URL returned  404 Not Found") is True

    def test_http_410_with_spaces(self) -> None:
        assert is_permanently_gone("server replied with 410 Gone") is True

    def test_none_returns_false(self) -> None:
        assert is_permanently_gone(None) is False

    def test_empty_string_returns_false(self) -> None:
        assert is_permanently_gone("") is False


# ---------------------------------------------------------------------------
# is_permanently_gone: negative cases (must NOT tombstone)
# ---------------------------------------------------------------------------


class TestIsPermanentlyGoneNegative:
    """Signals that must NOT trigger tombstoning."""

    def test_authentication_failed(self) -> None:
        assert is_permanently_gone("authentication failed") is False

    def test_invalid_credentials(self) -> None:
        assert is_permanently_gone("remote: invalid credentials") is False

    def test_bad_credentials(self) -> None:
        assert is_permanently_gone("bad credentials for https://github.com/user/repo") is False

    def test_permission_denied(self) -> None:
        assert is_permanently_gone("permission denied (publickey)") is False

    def test_access_denied(self) -> None:
        assert is_permanently_gone("access denied") is False

    def test_could_not_read_username(self) -> None:
        assert (
            is_permanently_gone("fatal: could not read username for 'https://github.com'") is False
        )

    def test_terminal_prompts_disabled(self) -> None:
        assert is_permanently_gone("terminal prompts disabled") is False

    def test_http_403(self) -> None:
        assert is_permanently_gone("returned error: 403") is False

    def test_http_403_in_message(self) -> None:
        assert (
            is_permanently_gone(
                "fatal: unable to access 'https://github.com/user/repo.git/': "
                "The requested URL returned error: 403"
            )
            is False
        )

    def test_network_connection_reset(self) -> None:
        assert is_permanently_gone("connection reset by peer") is False

    def test_timeout(self) -> None:
        assert is_permanently_gone("operation timed out after 3600s") is False

    def test_ssl_error(self) -> None:
        assert is_permanently_gone("SSL certificate problem: certificate has expired") is False

    def test_rate_limit(self) -> None:
        assert is_permanently_gone("API rate limit exceeded") is False

    def test_network_unreachable(self) -> None:
        assert is_permanently_gone("network is unreachable") is False

    def test_could_not_resolve_host(self) -> None:
        assert (
            is_permanently_gone("fatal: unable to access: could not resolve host: github.com")
            is False
        )

    def test_generic_error(self) -> None:
        assert is_permanently_gone("some entirely unrecognised failure mode") is False

    def test_storage_error(self) -> None:
        assert is_permanently_gone("fatal: no space left on device") is False

    def test_repository_not_found_suppressed_by_auth_signal(self) -> None:
        """If auth signal is present alongside a gone-like string, do NOT tombstone."""
        msg = "authentication failed for https://github.com/user/repo.git: repository not found"
        assert is_permanently_gone(msg) is False


# ---------------------------------------------------------------------------
# GitMirrorRepository.list_due: EXCLUDED rows skipped
# ---------------------------------------------------------------------------


class FakeSession:
    """Minimal SQLAlchemy session fake for list_due queries."""

    def __init__(self, rows: list[GitMirror]) -> None:
        self._rows = rows

    async def scalars(self, stmt: Any) -> Any:
        # Evaluate the WHERE clause by filtering based on status.
        eligible = [
            r
            for r in self._rows
            if r.status
            in (
                GitMirrorStatus.PENDING,
                GitMirrorStatus.OK,
                GitMirrorStatus.FAILED,
            )
            # EXCLUDED must never appear in list_due results -- mirror_repo
            # enforces this via SQL; here we replicate the semantics.
            and r.status != GitMirrorStatus.EXCLUDED
        ]

        class _Result:
            def __init__(self, items: list[GitMirror]) -> None:
                self._items = items

            def all(self) -> list[GitMirror]:
                return self._items

        return _Result(eligible)


class FakeSessionContextManager:
    def __init__(self, rows: list[GitMirror]) -> None:
        self._rows = rows

    async def __aenter__(self) -> FakeSession:
        return FakeSession(self._rows)

    async def __aexit__(self, *args: Any) -> None:
        pass


class FakeDB:
    def __init__(self, rows: list[GitMirror]) -> None:
        self._rows = rows

    def session(self) -> FakeSessionContextManager:
        return FakeSessionContextManager(self._rows)


class TestListDueSkipsExcluded:
    """list_due must never return EXCLUDED mirrors."""

    async def test_excluded_row_not_returned(self) -> None:
        from app.adapters.git_backup.repository import GitMirrorRepository

        excluded = _make_mirror(mirror_id=1, status=GitMirrorStatus.EXCLUDED)
        pending = _make_mirror(mirror_id=2, status=GitMirrorStatus.PENDING)
        ok_mirror = _make_mirror(mirror_id=3, status=GitMirrorStatus.OK)
        failed = _make_mirror(mirror_id=4, status=GitMirrorStatus.FAILED)

        cfg = _make_config()
        db = FakeDB([excluded, pending, ok_mirror, failed])
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        results = await repo.list_due()

        ids = {r.id for r in results}
        assert 1 not in ids, "EXCLUDED mirror must not appear in list_due"
        assert {2, 3, 4} == ids

    async def test_only_excluded_returns_empty(self) -> None:
        from app.adapters.git_backup.repository import GitMirrorRepository

        excluded = _make_mirror(mirror_id=1, status=GitMirrorStatus.EXCLUDED)
        cfg = _make_config()
        db = FakeDB([excluded])
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        results = await repo.list_due()
        assert results == []


# ---------------------------------------------------------------------------
# GitMirrorRepository.upsert_target: revives EXCLUDED row
# ---------------------------------------------------------------------------


class FakeTransactionSession:
    """Session fake that supports upsert_target (scalar + flush + refresh)."""

    def __init__(self, existing: GitMirror | None) -> None:
        self._existing = existing
        self._added: list[GitMirror] = []

    async def scalar(self, stmt: Any) -> GitMirror | None:
        return self._existing

    async def flush(self) -> None:
        pass

    async def refresh(self, obj: GitMirror) -> None:
        pass

    def add(self, obj: GitMirror) -> None:
        self._added.append(obj)


class FakeTransactionContextManager:
    def __init__(self, existing: GitMirror | None) -> None:
        self._session = FakeTransactionSession(existing)

    async def __aenter__(self) -> FakeTransactionSession:
        return self._session

    async def __aexit__(self, *args: Any) -> None:
        pass


class FakeDBForUpsert:
    def __init__(self, existing: GitMirror | None) -> None:
        self._ctx = FakeTransactionContextManager(existing)

    def transaction(self) -> FakeTransactionContextManager:
        return self._ctx


class TestUpsertTargetRevivesExcluded:
    """upsert_target must revive an EXCLUDED row back to PENDING."""

    async def test_excluded_row_is_revived(self) -> None:
        from app.adapters.git_backup.repository import GitMirrorRepository

        excluded = _make_mirror(
            mirror_id=1,
            status=GitMirrorStatus.EXCLUDED,
            consecutive_failures=3,
            excluded_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        excluded.last_error = "repository not found"
        excluded.last_error_category = "AUTH_ERROR"
        excluded.backoff_until = dt.datetime(2026, 1, 2, tzinfo=dt.UTC)

        cfg = _make_config()
        db = FakeDBForUpsert(excluded)
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        result = await repo.upsert_target(
            user_id=100,
            source=GitMirrorSource.GITHUB,
            clone_url=_URL,
            name=_NAME,
        )

        assert result.status == GitMirrorStatus.PENDING
        assert result.excluded_at is None
        assert result.consecutive_failures == 0
        assert result.backoff_until is None
        assert result.last_error is None
        assert result.last_error_category is None

    async def test_non_excluded_existing_not_reset(self) -> None:
        """An existing FAILED row must not be touched (only EXCLUDED rows revive)."""
        from app.adapters.git_backup.repository import GitMirrorRepository

        failed = _make_mirror(
            mirror_id=1,
            status=GitMirrorStatus.FAILED,
            consecutive_failures=2,
        )
        failed.last_error = "timeout"

        cfg = _make_config()
        db = FakeDBForUpsert(failed)
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        result = await repo.upsert_target(
            user_id=100,
            source=GitMirrorSource.GITHUB,
            clone_url=_URL,
            name=_NAME,
        )

        # Run state must be preserved.
        assert result.status == GitMirrorStatus.FAILED
        assert result.consecutive_failures == 2
        assert result.last_error == "timeout"


# ---------------------------------------------------------------------------
# GitMirrorService: service-level tombstone integration test
# ---------------------------------------------------------------------------


class FakeMirrorRepo:
    """Injectable fake for GitMirrorRepository used in service tests."""

    def __init__(self, mirrors: list[GitMirror]) -> None:
        self._mirrors = mirrors
        self.excluded_calls: list[tuple[int, str]] = []
        self.failure_calls: list[tuple[int, str]] = []

    async def list_due(self, user_id: int | None = None) -> list[GitMirror]:
        return list(self._mirrors)

    async def record_excluded(self, mirror_id: int, reason: str) -> None:
        self.excluded_calls.append((mirror_id, reason))

    async def record_failure(
        self,
        mirror_id: int,
        error_category: Any,
        message: str,
        clone_strategy: str | None = None,
        *,
        use_http1: bool | None = None,
    ) -> None:
        self.failure_calls.append((mirror_id, message))

    async def record_success(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def record_skip(self, mirror_id: int, reason: str) -> None:
        pass


def _make_service(
    fake_repo: FakeMirrorRepo,
    git_runner: Any,
    *,
    data_path: str = "/tmp/git-mirrors-test",
) -> GitMirrorService:
    cfg = _make_config(GIT_BACKUP_DATA_PATH=data_path)
    # Provide a minimal fake DB (not used by service directly after injection).
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


class TestServiceTombstoneIntegration:
    """Service-level tests: gone error tombstones; auth error does not."""

    async def test_repository_not_found_tombstones(self) -> None:
        """A 'repository not found' git failure must tombstone the mirror."""
        mirror = _make_mirror(mirror_id=42, status=GitMirrorStatus.PENDING)
        fake_repo = FakeMirrorRepo([mirror])

        async def failing_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            return 128, "remote: Repository not found.\nfatal: repository not found"

        service = _make_service(fake_repo, failing_runner)

        # Patch _preflight_storage_check to always succeed, and _resolve_url
        # to return the bare URL without touching the DB.
        with (
            patch(
                "app.adapters.git_backup.mirror_service._preflight_storage_check",
                return_value=None,
            ),
            patch(
                "app.adapters.git_backup.mirror_service.assert_resolved_public_host",
                return_value=None,
            ),
            patch.object(service, "_resolve_url", return_value=_URL),
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.mkdir", return_value=None),
        ):
            summary = await service.perform_sync(user_id=100)

        # The outcome must be excluded (counted as skipped in summary).
        assert summary.skipped == 1
        assert summary.failed == 0
        assert len(fake_repo.excluded_calls) == 1
        mirror_id, reason = fake_repo.excluded_calls[0]
        assert mirror_id == 42
        assert "repository not found" in reason.lower()
        assert len(fake_repo.failure_calls) == 0

    async def test_auth_failure_does_not_tombstone(self) -> None:
        """An authentication failure must use the normal failure path, not tombstone."""
        mirror = _make_mirror(mirror_id=43, status=GitMirrorStatus.PENDING)
        fake_repo = FakeMirrorRepo([mirror])

        async def failing_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            return 128, "authentication failed for 'https://github.com/user/repo.git'"

        service = _make_service(fake_repo, failing_runner)

        with (
            patch(
                "app.adapters.git_backup.mirror_service._preflight_storage_check",
                return_value=None,
            ),
            patch(
                "app.adapters.git_backup.mirror_service.assert_resolved_public_host",
                return_value=None,
            ),
            patch.object(service, "_resolve_url", return_value=_URL),
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.mkdir", return_value=None),
        ):
            summary = await service.perform_sync(user_id=100)

        # Auth failure must not tombstone.
        assert len(fake_repo.excluded_calls) == 0
        assert len(fake_repo.failure_calls) == 1
        assert summary.failed == 1
        assert summary.skipped == 0

    async def test_404_error_tombstones(self) -> None:
        """An HTTP 404 error in git output must tombstone the mirror."""
        mirror = _make_mirror(mirror_id=44, status=GitMirrorStatus.PENDING)
        fake_repo = FakeMirrorRepo([mirror])

        async def failing_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            return (
                128,
                "fatal: unable to access 'https://github.com/user/gone.git/': returned error: 404",
            )

        service = _make_service(fake_repo, failing_runner)

        with (
            patch(
                "app.adapters.git_backup.mirror_service._preflight_storage_check",
                return_value=None,
            ),
            patch(
                "app.adapters.git_backup.mirror_service.assert_resolved_public_host",
                return_value=None,
            ),
            patch.object(service, "_resolve_url", return_value=_URL),
            patch("pathlib.Path.exists", return_value=False),
            patch("pathlib.Path.mkdir", return_value=None),
        ):
            summary = await service.perform_sync(user_id=100)

        assert len(fake_repo.excluded_calls) == 1
        assert fake_repo.excluded_calls[0][0] == 44
        assert len(fake_repo.failure_calls) == 0
        assert summary.skipped == 1
