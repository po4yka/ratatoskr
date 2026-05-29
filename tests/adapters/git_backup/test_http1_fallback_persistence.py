"""Tests for per-mirror HTTP/1.1 fallback persistence (use_http1_fallback column).

Covers:
(a) A mirror with use_http1_fallback=True causes the first build_git_command call
    to pass force_http1=True.
(b) An HTTP2_ERROR failure sets use_http1_fallback=True via record_failure.
(c) record_success clears use_http1_fallback=False.

All tests are hermetic: no real DB, no filesystem, no subprocess calls.
"""

from __future__ import annotations

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


def _make_config(**overrides: Any) -> GitBackupConfig:
    base: dict[str, Any] = {
        "GIT_BACKUP_ENABLED": False,
        "GIT_BACKUP_DATA_PATH": "/tmp/git-http1-test",
    }
    base.update(overrides)
    return GitBackupConfig.model_validate(base)


def _make_mirror(
    *,
    mirror_id: int = 1,
    status: GitMirrorStatus = GitMirrorStatus.PENDING,
    consecutive_failures: int = 0,
    use_http1_fallback: bool = False,
) -> GitMirror:
    m = GitMirror(
        id=mirror_id,
        user_id=100,
        source=GitMirrorSource.GITHUB,
        clone_url="https://github.com/octocat/test-repo.git",
        name="octocat/test-repo",
        consecutive_failures=consecutive_failures,
        status=status,
    )
    m.use_http1_fallback = use_http1_fallback
    return m


# ---------------------------------------------------------------------------
# Fake DB helpers (matching the tombstone test style)
# ---------------------------------------------------------------------------


class _FakeTransactionSession:
    """Session fake that supports record_success / record_failure (scalar + mutations)."""

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
# FakeMirrorRepo for service-level tests
# ---------------------------------------------------------------------------


class _FakeMirrorRepo:
    """Injectable fake for GitMirrorRepository used in service tests."""

    def __init__(self, mirrors: list[GitMirror]) -> None:
        self._mirrors = mirrors
        self.success_calls: list[dict[str, Any]] = []
        self.failure_calls: list[dict[str, Any]] = []
        self.excluded_calls: list[tuple[int, str]] = []

    async def list_due(self, user_id: int | None = None) -> list[GitMirror]:
        return list(self._mirrors)

    async def record_success(
        self,
        mirror_id: int,
        mirror_path: str,
        size_kb: int | None,
        default_branch: str | None,
        clone_strategy: str | None = None,
    ) -> None:
        self.success_calls.append(
            {
                "mirror_id": mirror_id,
                "mirror_path": mirror_path,
                "size_kb": size_kb,
            }
        )

    async def record_failure(
        self,
        mirror_id: int,
        error_category: ErrorCategory,
        message: str,
        clone_strategy: str | None = None,
        *,
        use_http1: bool | None = None,
    ) -> None:
        self.failure_calls.append(
            {
                "mirror_id": mirror_id,
                "error_category": error_category,
                "message": message,
                "use_http1": use_http1,
            }
        )

    async def record_excluded(self, mirror_id: int, reason: str) -> None:
        self.excluded_calls.append((mirror_id, reason))

    async def record_skip(self, mirror_id: int, reason: str) -> None:
        pass


def _make_service(
    fake_repo: _FakeMirrorRepo,
    git_runner: Any,
    *,
    data_path: str = "/tmp/git-mirrors-test",
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


_PATCH_PREFLIGHT = patch(
    "app.adapters.git_backup.mirror_service._preflight_storage_check",
    return_value=None,
)
_PATCH_DNS = patch(
    "app.adapters.git_backup.mirror_service.assert_resolved_public_host",
    return_value=None,
)
_URL = "https://github.com/octocat/test-repo.git"

# ---------------------------------------------------------------------------
# (a) DB flag seeds force_http1 on the first attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_http1_flag_seeds_force_http1_on_first_attempt() -> None:
    """When use_http1_fallback=True on the mirror row, force_http1 must be True
    on the very first build_git_command call (before any retry escalation)."""
    mirror = _make_mirror(mirror_id=1, use_http1_fallback=True)
    fake_repo = _FakeMirrorRepo([mirror])

    captured_force_http1: list[bool] = []

    async def fake_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
        return 0, ""

    service = _make_service(fake_repo, fake_runner)

    original_build = None

    def _capturing_build(**kwargs: Any) -> list[str]:
        captured_force_http1.append(bool(kwargs.get("force_http1", False)))
        # Return a minimal valid argv so git_runner doesn't blow up.
        return ["git", "clone", "--mirror", _URL]

    import app.adapters.git_backup.mirror_service as _svc_mod

    with (
        _PATCH_PREFLIGHT,
        _PATCH_DNS,
        patch.object(service, "_resolve_url", return_value=_URL),
        patch("pathlib.Path.exists", return_value=False),
        patch("pathlib.Path.mkdir", return_value=None),
        patch.object(_svc_mod, "build_git_command", side_effect=_capturing_build),
    ):
        await service.perform_sync(user_id=100)

    assert len(captured_force_http1) >= 1, "build_git_command must have been called"
    assert captured_force_http1[0] is True, (
        "First attempt must use force_http1=True when mirror.use_http1_fallback is set"
    )


@pytest.mark.asyncio
async def test_db_http1_flag_false_does_not_force_http1_on_first_attempt() -> None:
    """When use_http1_fallback=False (the default), force_http1 must be False on attempt 1."""
    mirror = _make_mirror(mirror_id=2, use_http1_fallback=False)
    fake_repo = _FakeMirrorRepo([mirror])

    captured_force_http1: list[bool] = []

    async def fake_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
        return 0, ""

    service = _make_service(fake_repo, fake_runner)

    def _capturing_build(**kwargs: Any) -> list[str]:
        captured_force_http1.append(bool(kwargs.get("force_http1", False)))
        return ["git", "clone", "--mirror", _URL]

    import app.adapters.git_backup.mirror_service as _svc_mod

    with (
        _PATCH_PREFLIGHT,
        _PATCH_DNS,
        patch.object(service, "_resolve_url", return_value=_URL),
        patch("pathlib.Path.exists", return_value=False),
        patch("pathlib.Path.mkdir", return_value=None),
        patch.object(_svc_mod, "build_git_command", side_effect=_capturing_build),
    ):
        await service.perform_sync(user_id=100)

    assert len(captured_force_http1) >= 1
    assert captured_force_http1[0] is False, (
        "First attempt must NOT force HTTP/1.1 when use_http1_fallback=False"
    )


# ---------------------------------------------------------------------------
# (b) HTTP/2 failure persists use_http1_fallback=True via record_failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http2_error_sets_use_http1_via_record_failure() -> None:
    """A git failure classified as HTTP2_ERROR must call record_failure with use_http1=True."""
    mirror = _make_mirror(mirror_id=3, use_http1_fallback=False)
    fake_repo = _FakeMirrorRepo([mirror])

    async def failing_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
        # Produce output that classify() maps to HTTP2_ERROR.
        return 1, "fatal: unable to access 'https://github.com/...': HTTP/2 stream 1 was not closed cleanly before end of the underlying stream"

    service = _make_service(fake_repo, failing_runner)

    with (
        _PATCH_PREFLIGHT,
        _PATCH_DNS,
        patch.object(service, "_resolve_url", return_value=_URL),
        patch("pathlib.Path.exists", return_value=False),
        patch("pathlib.Path.mkdir", return_value=None),
    ):
        summary = await service.perform_sync(user_id=100)

    assert summary.failed == 1
    assert len(fake_repo.failure_calls) == 1
    call = fake_repo.failure_calls[0]
    assert call["mirror_id"] == 3
    assert call["error_category"] == ErrorCategory.HTTP2_ERROR
    assert call["use_http1"] is True, (
        "record_failure must be called with use_http1=True for HTTP2_ERROR"
    )


@pytest.mark.asyncio
async def test_non_http2_error_does_not_set_use_http1() -> None:
    """A network error (non-HTTP/2) must call record_failure with use_http1=None,
    leaving the existing flag unchanged."""
    mirror = _make_mirror(mirror_id=4, use_http1_fallback=False)
    fake_repo = _FakeMirrorRepo([mirror])

    async def failing_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
        return 1, "fatal: could not read Username: terminal prompts disabled"

    service = _make_service(fake_repo, failing_runner)

    with (
        _PATCH_PREFLIGHT,
        _PATCH_DNS,
        patch.object(service, "_resolve_url", return_value=_URL),
        patch("pathlib.Path.exists", return_value=False),
        patch("pathlib.Path.mkdir", return_value=None),
    ):
        await service.perform_sync(user_id=100)

    assert len(fake_repo.failure_calls) == 1
    call = fake_repo.failure_calls[0]
    assert call["use_http1"] is None, (
        "record_failure must not set use_http1 for non-HTTP/2 errors"
    )


# ---------------------------------------------------------------------------
# (c) record_success clears use_http1_fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_success_clears_use_http1_fallback() -> None:
    """record_success must set use_http1_fallback=False on the row."""
    from app.adapters.git_backup.repository import GitMirrorRepository

    row = _make_mirror(mirror_id=5, use_http1_fallback=True)
    db = _FakeDB(row)
    cfg = _make_config()
    repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

    await repo.record_success(
        mirror_id=5,
        mirror_path="/data/test.git",
        size_kb=1024,
        default_branch="main",
    )

    assert row.use_http1_fallback is False, (
        "record_success must clear use_http1_fallback after a clean sync"
    )


def test_record_success_clears_use_http1_fallback_sync() -> None:
    """Synchronous variant: verify the mutation is applied to the in-memory row."""
    import asyncio

    row = _make_mirror(mirror_id=6, use_http1_fallback=True)

    async def _run() -> None:
        from app.adapters.git_backup.repository import GitMirrorRepository

        db = _FakeDB(row)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]
        await repo.record_success(
            mirror_id=6,
            mirror_path="/data/test2.git",
            size_kb=512,
            default_branch=None,
        )

    asyncio.get_event_loop().run_until_complete(_run())
    assert row.use_http1_fallback is False


# ---------------------------------------------------------------------------
# (d) record_failure with use_http1=True sets the flag; None leaves it unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_failure_with_use_http1_true_sets_flag() -> None:
    """record_failure(use_http1=True) must set use_http1_fallback=True on the row."""
    from app.adapters.git_backup.repository import GitMirrorRepository

    row = _make_mirror(mirror_id=7, use_http1_fallback=False)
    db = _FakeDB(row)
    cfg = _make_config()
    repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

    await repo.record_failure(
        mirror_id=7,
        error_category=ErrorCategory.HTTP2_ERROR,
        message="HTTP/2 stream error",
        use_http1=True,
    )

    assert row.use_http1_fallback is True, (
        "record_failure(use_http1=True) must set use_http1_fallback on the row"
    )


@pytest.mark.asyncio
async def test_record_failure_with_use_http1_none_leaves_flag_unchanged() -> None:
    """record_failure(use_http1=None) must not touch use_http1_fallback."""
    from app.adapters.git_backup.repository import GitMirrorRepository

    row = _make_mirror(mirror_id=8, use_http1_fallback=True)
    db = _FakeDB(row)
    cfg = _make_config()
    repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

    await repo.record_failure(
        mirror_id=8,
        error_category=ErrorCategory.NETWORK_ERROR,
        message="connection reset by peer",
        use_http1=None,
    )

    # Flag must be unchanged (was True before call).
    assert row.use_http1_fallback is True, (
        "record_failure(use_http1=None) must leave use_http1_fallback unchanged"
    )


@pytest.mark.asyncio
async def test_record_failure_network_error_sets_use_http1() -> None:
    """NETWORK_ERROR also belongs to _HTTP1_FALLBACK set, so use_http1=True must be passed."""
    mirror = _make_mirror(mirror_id=9, use_http1_fallback=False)
    fake_repo = _FakeMirrorRepo([mirror])

    async def failing_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
        return 1, "fatal: unable to access: connection reset by peer"

    service = _make_service(fake_repo, failing_runner)

    with (
        _PATCH_PREFLIGHT,
        _PATCH_DNS,
        patch.object(service, "_resolve_url", return_value=_URL),
        patch("pathlib.Path.exists", return_value=False),
        patch("pathlib.Path.mkdir", return_value=None),
    ):
        await service.perform_sync(user_id=100)

    assert len(fake_repo.failure_calls) == 1
    assert fake_repo.failure_calls[0]["use_http1"] is True, (
        "NETWORK_ERROR is in _HTTP1_FALLBACK; record_failure must receive use_http1=True"
    )
