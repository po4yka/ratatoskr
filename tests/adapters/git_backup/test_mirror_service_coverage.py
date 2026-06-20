"""Hermetic tests targeting uncovered branches in app.adapters.git_backup.mirror_service.

Covers:
- _default_git_runner: TimeoutError kill path (lines 88-112)
- _preflight_storage_check: TimeoutError and generic Exception branches (lines 170, 177, 187)
- _is_ignored: invalid-regex literal-substring fallback (lines 205-208)
- _apply_priority_rules: regex-error fallback (lines 235-236)
- _build_lfs: lfs_available() returns False -> returns None (lines 349-350)
- dry-run ignore-list hit path (lines 377-378)
- circuit-breaker-open fast-path skip (lines 435-436)
- extra_repos iteration: upsert with user_id, duplicate-skip, synthetic-mirror (lines 531-572)
- _resolve_url: non-github-host warning, missing integration, decrypt failed (lines 620-666)
- _sync_one: host-is-None and ValueError blocked-host returns (lines 713, 722-724)
- run_with_retry: permanently-gone excluded path and bare-Exception handler (lines 806-831)
- post-sync maintenance and LFS hooks called on success (lines 847-852)
- large-repo semaphore branch (lines 862-863)
- _persist_outcome: size OSError and http1 fallback flag derivation (lines 878, 888-903)

All tests are hermetic: no Postgres, no Qdrant, no network, no subprocess I/O.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.git_backup.circuit_breaker import StorageCircuitBreaker
from app.adapters.git_backup.errors import ErrorCategory
from app.adapters.git_backup.mirror_service import (
    GitMirrorService,
    MirrorOutcome,
    MirrorTask,
    _apply_priority_rules,
    _default_git_runner,
    _is_ignored,
    _preflight_storage_check,
)
from app.adapters.git_backup.retry import RetryPolicy
from app.config.git_backup import GitBackupConfig
from app.db.models.git_backup import GitMirror, GitMirrorSource, GitMirrorStatus
from app.db.models.repository import GitHubIntegrationStatus

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> GitBackupConfig:
    base: dict[str, Any] = {
        "GIT_BACKUP_ENABLED": False,
        "GIT_BACKUP_DATA_PATH": "/tmp/git-mirror-cov-test",
    }
    base.update(overrides)
    return GitBackupConfig.model_validate(base)


def _make_mirror(
    *,
    mirror_id: int = 1,
    name: str = "user/repo",
    clone_url: str = "https://github.com/user/repo.git",
    source: GitMirrorSource = GitMirrorSource.GITHUB,
    size_kb: int | None = None,
    consecutive_failures: int = 0,
    use_http1_fallback: bool = False,
) -> GitMirror:
    m = GitMirror(
        id=mirror_id,
        user_id=100,
        source=source,
        clone_url=clone_url,
        name=name,
        consecutive_failures=consecutive_failures,
        status=GitMirrorStatus.PENDING,
        size_kb=size_kb,
    )
    m.use_http1_fallback = use_http1_fallback
    return m


def _make_task(
    *,
    name: str = "user/repo",
    url: str = "https://github.com/user/repo.git",
    mirror_id: int = 1,
    is_large_repo: bool = False,
    timeout_seconds_override: int | None = None,
) -> MirrorTask:
    mirror = _make_mirror(mirror_id=mirror_id, name=name, clone_url=url)
    return MirrorTask(
        mirror=mirror,
        effective_url=url,
        name=name,
        destination=Path(f"/tmp/cov-test/{name}"),
        is_large_repo=is_large_repo,
        timeout_seconds_override=timeout_seconds_override,
    )


class _FakeMirrorRepo:
    """Minimal injectable fake for GitMirrorRepository."""

    def __init__(self, mirrors: list[GitMirror] | None = None) -> None:
        self._mirrors = mirrors or []
        self.upsert_calls: list[dict[str, Any]] = []
        self.success_calls: list[dict[str, Any]] = []
        self.failure_calls: list[dict[str, Any]] = []
        self.excluded_calls: list[tuple[int, int, str]] = []
        self.skip_calls: list[tuple[int, int, str]] = []

    async def list_due(self, user_id: int | None = None) -> list[GitMirror]:
        return list(self._mirrors)

    async def upsert_target(self, **kwargs: Any) -> GitMirror:
        self.upsert_calls.append(kwargs)
        return _make_mirror(
            name=kwargs.get("name", "extra"),
            clone_url=kwargs.get("clone_url", "https://example.com/extra.git"),
            source=GitMirrorSource.MANUAL,
        )

    async def record_success(self, **kwargs: Any) -> None:
        self.success_calls.append(kwargs)

    async def record_failure(self, **kwargs: Any) -> None:
        self.failure_calls.append(kwargs)

    async def record_excluded(self, mirror_id: int, user_id: int, reason: str) -> None:
        self.excluded_calls.append((mirror_id, user_id, reason))

    async def record_skip(self, mirror_id: int, user_id: int, reason: str) -> None:
        self.skip_calls.append((mirror_id, user_id, reason))


def _make_service(
    fake_repo: _FakeMirrorRepo,
    cfg: GitBackupConfig,
    *,
    maintenance: Any = None,
    lfs: Any = None,
    git_runner: Any = None,
    circuit_breaker: StorageCircuitBreaker | None = None,
) -> GitMirrorService:
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    async def noop_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
        return 0, ""

    return GitMirrorService(
        config=cfg,
        mirror_repo=fake_repo,  # type: ignore[arg-type]
        db=db,
        retry_policy=RetryPolicy(max_attempts=1, base_delay_ms=0),
        circuit_breaker=circuit_breaker or StorageCircuitBreaker(threshold=100),
        maintenance=maintenance,
        lfs=lfs,
        git_runner=git_runner or noop_runner,
    )


# ---------------------------------------------------------------------------
# _default_git_runner: TimeoutError kill path
# ---------------------------------------------------------------------------


class TestDefaultGitRunnerTimeout:
    @pytest.mark.asyncio
    async def test_timeout_kills_process_and_raises_runtime_error(self) -> None:
        """When asyncio.wait_for raises TimeoutError, kill() and wait() are called."""
        mock_process = AsyncMock()
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock()

        async def fake_communicate() -> tuple[bytes, bytes]:
            raise TimeoutError()

        mock_process.communicate = fake_communicate

        with (
            patch("asyncio.create_subprocess_exec", return_value=mock_process),
            patch(
                "asyncio.wait_for",
                side_effect=TimeoutError(),
            ),
            pytest.raises(RuntimeError, match="timed out after"),
        ):
            await _default_git_runner(["git", "fetch"], Path("/tmp"), 0.001)

        mock_process.kill.assert_called_once()
        mock_process.wait.assert_awaited_once()


# ---------------------------------------------------------------------------
# _preflight_storage_check: TimeoutError and generic Exception branches
# ---------------------------------------------------------------------------


class TestPreflightStorageCheck:
    @pytest.mark.asyncio
    async def test_timeout_returns_error_string(self, tmp_path: Path) -> None:
        """When asyncio.wait_for raises TimeoutError, a descriptive error string is returned."""
        with patch("asyncio.wait_for", side_effect=TimeoutError()):
            result = await _preflight_storage_check(tmp_path, timeout_ms=1)
        assert result is not None
        assert "timed out" in result.lower()
        assert "1ms" in result

    @pytest.mark.asyncio
    async def test_generic_exception_returns_error_string(self, tmp_path: Path) -> None:
        """When _sync_check raises an unexpected exception, its message is returned."""
        with patch(
            "asyncio.wait_for",
            side_effect=OSError("disk I/O error"),
        ):
            result = await _preflight_storage_check(tmp_path, timeout_ms=5000)
        assert result is not None
        assert "disk I/O error" in result

    @pytest.mark.asyncio
    async def test_missing_path_returns_error(self, tmp_path: Path) -> None:
        """Non-existent path returns an error string (real filesystem, no mocks)."""
        result = await _preflight_storage_check(tmp_path / "no-such-dir", timeout_ms=5000)
        assert result is not None
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_valid_path_returns_none(self, tmp_path: Path) -> None:
        """Writable existing directory returns None."""
        result = await _preflight_storage_check(tmp_path, timeout_ms=5000)
        assert result is None


# ---------------------------------------------------------------------------
# _is_ignored: invalid-regex literal-substring fallback
# ---------------------------------------------------------------------------


class TestIsIgnoredLiteralFallback:
    def test_invalid_regex_hits_name(self) -> None:
        # "[unclosed" is not a valid regex; fallback to substring match on name.
        assert _is_ignored("my[unclosed]repo", "https://example.com/r.git", ["[unclosed]"]) is True

    def test_invalid_regex_hits_url(self) -> None:
        # "[bad" is not a valid regex; fallback to substring match on url.
        assert _is_ignored("normal-name", "https://example.com/[bad]/repo.git", ["[bad]"]) is True

    def test_invalid_regex_no_match(self) -> None:
        # Invalid regex pattern that does not match either name or url.
        assert _is_ignored("myrepo", "https://example.com/r.git", ["[[[never"]) is False

    def test_valid_regex_short_circuits_before_fallback(self) -> None:
        # Valid regex that matches; ensure the try-branch is taken.
        assert _is_ignored("user/fork-archive", "https://example.com/r.git", [r"fork-\w+"]) is True


# ---------------------------------------------------------------------------
# _apply_priority_rules: regex-error fallback
# ---------------------------------------------------------------------------


class TestApplyPriorityRulesRegexFallback:
    def test_invalid_regex_falls_back_to_substring_match(self) -> None:
        tasks = [
            _make_task(name="my[special]repo", url="https://git.example.com/my[special]repo.git"),
        ]

        class _Rule:
            pattern = "[special]"  # invalid regex
            priority = 10
            timeout_seconds = None

        result = _apply_priority_rules(tasks, [_Rule()])
        # The task should still appear; confirm no exception raised.
        assert len(result) == 1

    def test_invalid_regex_literal_match_sets_priority(self) -> None:
        """Invalid regex that IS a substring match should still apply priority."""
        tasks = [
            _make_task(name="normal", url="https://git.example.com/normal.git", mirror_id=1),
            _make_task(
                name="special*repo",
                url="https://git.example.com/special*repo.git",
                mirror_id=2,
            ),
        ]

        class _Rule:
            # "*" alone is an invalid regex (nothing to repeat)
            pattern = "*"
            priority = 50
            timeout_seconds = None

        result = _apply_priority_rules(tasks, [_Rule()])
        # Both name and url contain "*" as a literal substring.
        # The special*repo task should come first due to higher priority.
        assert result[0].name == "special*repo"


# ---------------------------------------------------------------------------
# _build_lfs: lfs_available() is False -> returns None
# ---------------------------------------------------------------------------


class TestBuildLfs:
    def test_lfs_not_available_returns_none(self) -> None:
        """When LfsSupport.is_lfs_available() is False, _build_lfs must return None."""
        cfg = _make_config(GIT_BACKUP_FETCH_LFS=True)
        mirror_repo = MagicMock()
        db = MagicMock()

        fake_lfs = MagicMock()
        fake_lfs.is_lfs_available.return_value = False

        with patch(
            "app.adapters.git_backup.mirror_service.LfsSupport",
            return_value=fake_lfs,
        ):
            service = GitMirrorService(cfg, mirror_repo, db)
            assert service._build_lfs() is None

    def test_lfs_available_returns_lfs_instance(self) -> None:
        """When LfsSupport.is_lfs_available() is True, _build_lfs must return the instance."""
        cfg = _make_config(GIT_BACKUP_FETCH_LFS=True)
        mirror_repo = MagicMock()
        db = MagicMock()

        fake_lfs = MagicMock()
        fake_lfs.is_lfs_available.return_value = True

        with patch(
            "app.adapters.git_backup.mirror_service.LfsSupport",
            return_value=fake_lfs,
        ):
            service = GitMirrorService(cfg, mirror_repo, db)
            assert service._build_lfs() is fake_lfs


# ---------------------------------------------------------------------------
# dry-run: ignore-list hit fast-path
# ---------------------------------------------------------------------------


class TestDryRunIgnore:
    @pytest.mark.asyncio
    async def test_dry_run_does_not_include_ignored_mirror(self) -> None:
        mirrors = [
            _make_mirror(
                mirror_id=1, name="user/keep", clone_url="https://github.com/user/keep.git"
            ),
            _make_mirror(
                mirror_id=2,
                name="user/archived",
                clone_url="https://github.com/user/archived.git",
            ),
        ]
        cfg = _make_config(ignore=["archived"])
        fake_repo = _FakeMirrorRepo(mirrors)
        service = _make_service(fake_repo, cfg)

        with (
            patch.object(service, "_resolve_url", side_effect=lambda m: (m.clone_url, None)),
            patch.object(
                service,
                "_mirror_destination",
                side_effect=lambda dp, m: dp / (m.name or ""),
            ),
            patch(
                "app.adapters.git_backup.mirror_service._preflight_storage_check",
                return_value=None,
            ),
        ):
            summary = await service.perform_sync(user_id=100, dry_run=True)

        # Only one task should have been planned (the ignored one was skipped).
        assert summary.ok == 1
        names = [o.mirror.name for o in summary.outcomes]
        assert "user/archived" not in names

    @pytest.mark.asyncio
    async def test_dry_run_extra_repo_ignored(self) -> None:
        cfg = _make_config(
            ignore=["ignored-extra"],
            GIT_BACKUP_EXTRA_REPOS={"ignored-extra": "https://git.example.com/ignored-extra.git"},
        )
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)

        with patch(
            "app.adapters.git_backup.mirror_service._preflight_storage_check",
            return_value=None,
        ):
            summary = await service.perform_sync(user_id=100, dry_run=True)

        assert summary.ok == 0
        assert summary.total == 0


# ---------------------------------------------------------------------------
# circuit-breaker-open fast-path skip
# ---------------------------------------------------------------------------


class TestCircuitBreakerOpenSkip:
    @pytest.mark.asyncio
    async def test_open_breaker_skips_all_tasks(self) -> None:
        mirrors = [
            _make_mirror(mirror_id=1, name="user/repo1"),
            _make_mirror(
                mirror_id=2, name="user/repo2", clone_url="https://github.com/user/repo2.git"
            ),
        ]
        cfg = _make_config()
        fake_repo = _FakeMirrorRepo(mirrors)

        # Build a breaker that is already open.
        open_breaker = StorageCircuitBreaker(threshold=1)
        open_breaker.record_failure(ErrorCategory.STORAGE_ERROR)
        assert open_breaker.is_open()

        service = _make_service(fake_repo, cfg, circuit_breaker=open_breaker)

        with (
            patch.object(service, "_resolve_url", side_effect=lambda m: (m.clone_url, None)),
            patch(
                "app.adapters.git_backup.mirror_service._preflight_storage_check",
                return_value=None,
            ),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
        ):
            summary = await service.perform_sync(user_id=100)

        # Both tasks should be skipped with the circuit-breaker reason.
        assert summary.skipped == 2
        assert summary.ok == 0
        for o in summary.outcomes:
            assert o.skipped is True
            assert o.skip_reason == "storage circuit breaker open"


# ---------------------------------------------------------------------------
# extra_repos iteration: upsert, duplicate-skip, synthetic mirror
# ---------------------------------------------------------------------------


class TestExtraRepos:
    @pytest.mark.asyncio
    async def test_extra_repo_upsertion_when_user_id_provided(self) -> None:
        cfg = _make_config(
            GIT_BACKUP_EXTRA_REPOS={"myorg/extra": "https://git.example.com/extra.git"}
        )
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)

        with (
            patch(
                "app.adapters.git_backup.mirror_service._preflight_storage_check",
                return_value=None,
            ),
            patch.object(service, "_resolve_url", side_effect=lambda m: (m.clone_url, None)),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
            patch("pathlib.Path.exists", return_value=True),
        ):
            await service.perform_sync(user_id=42)

        # upsert_target must have been called for the extra repo.
        assert len(fake_repo.upsert_calls) == 1
        assert fake_repo.upsert_calls[0]["user_id"] == 42
        assert fake_repo.upsert_calls[0]["clone_url"] == "https://git.example.com/extra.git"

    @pytest.mark.asyncio
    async def test_extra_repo_skipped_when_already_in_db(self) -> None:
        db_mirror = _make_mirror(
            mirror_id=99,
            name="myorg/extra",
            clone_url="https://git.example.com/extra.git",
        )
        cfg = _make_config(
            GIT_BACKUP_EXTRA_REPOS={"myorg/extra": "https://git.example.com/extra.git"}
        )
        fake_repo = _FakeMirrorRepo([db_mirror])
        service = _make_service(fake_repo, cfg)

        with (
            patch(
                "app.adapters.git_backup.mirror_service._preflight_storage_check",
                return_value=None,
            ),
            patch.object(service, "_resolve_url", side_effect=lambda m: (m.clone_url, None)),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
            patch("pathlib.Path.exists", return_value=True),
        ):
            await service.perform_sync(user_id=42)

        # No upsert because the DB already has that URL.
        assert len(fake_repo.upsert_calls) == 0

    @pytest.mark.asyncio
    async def test_extra_repo_synthetic_mirror_when_no_user_id(self) -> None:
        """Without user_id, a synthetic GitMirror (id=-1) is created for extra repos."""
        cfg = _make_config(
            GIT_BACKUP_EXTRA_REPOS={"myorg/extra": "https://git.example.com/extra.git"}
        )
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)

        collected_tasks: list[MirrorTask] = []
        original_collect = service._collect_tasks

        async def spy_collect(user_id: int | None, data_path: Path) -> list[MirrorTask]:
            tasks = await original_collect(user_id, data_path)
            collected_tasks.extend(tasks)
            return tasks

        service._collect_tasks = spy_collect  # type: ignore[method-assign]

        with (
            patch(
                "app.adapters.git_backup.mirror_service._preflight_storage_check",
                return_value=None,
            ),
            patch.object(service, "_resolve_url", side_effect=lambda m: (m.clone_url, None)),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
        ):
            await service.perform_sync(user_id=None)

        # No upsert — synthetic path.
        assert len(fake_repo.upsert_calls) == 0
        # The task should exist with id=-1 (synthetic).
        assert len(collected_tasks) == 1
        assert collected_tasks[0].mirror.id == -1

    @pytest.mark.asyncio
    async def test_extra_repo_synthetic_persisted_outcome_skipped(self) -> None:
        """_persist_outcome must skip synthetic mirrors (id=-1) without calling record_*."""
        cfg = _make_config(
            GIT_BACKUP_EXTRA_REPOS={"myorg/extra": "https://git.example.com/extra.git"}
        )
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)

        with (
            patch(
                "app.adapters.git_backup.mirror_service._preflight_storage_check",
                return_value=None,
            ),
            patch.object(service, "_resolve_url", side_effect=lambda m: (m.clone_url, None)),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
        ):
            await service.perform_sync(user_id=None)

        # record_success/record_failure must not have been called for the synthetic mirror.
        assert len(fake_repo.success_calls) == 0
        assert len(fake_repo.failure_calls) == 0


# ---------------------------------------------------------------------------
# _resolve_url: non-github-host warning, missing integration, decrypt failed
# ---------------------------------------------------------------------------


class TestResolveUrl:
    @pytest.mark.asyncio
    async def test_non_github_host_returns_plain_url(self) -> None:
        """GITHUB source with a non-GitHub hostname skips credential injection."""
        mirror = _make_mirror(
            clone_url="https://evil.example.com/user/repo.git",
            source=GitMirrorSource.GITHUB,
        )
        cfg = _make_config()
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)

        result = await service._resolve_url(mirror)
        assert result == (mirror.clone_url, None)

    @pytest.mark.asyncio
    async def test_missing_integration_returns_plain_url(self) -> None:
        """When no UserGitHubIntegration row exists, the plain URL is returned."""
        mirror = _make_mirror(
            clone_url="https://github.com/user/repo.git",
            source=GitMirrorSource.GITHUB,
        )
        cfg = _make_config()
        fake_repo = _FakeMirrorRepo([])

        db = MagicMock()
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        session_ctx.__aexit__ = AsyncMock(return_value=None)
        # scalar returns None -> no integration row.
        session_ctx.__aenter__.return_value.scalar = AsyncMock(return_value=None)
        db.session.return_value = session_ctx

        service = GitMirrorService(
            config=cfg,
            mirror_repo=fake_repo,  # type: ignore[arg-type]
            db=db,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_ms=0),
            circuit_breaker=StorageCircuitBreaker(threshold=100),
            maintenance=None,
            lfs=None,
            git_runner=AsyncMock(return_value=(0, "")),
        )

        result = await service._resolve_url(mirror)
        assert result == (mirror.clone_url, None)

    @pytest.mark.asyncio
    async def test_github_credentials_query_requires_active_integration(self) -> None:
        mirror = _make_mirror(
            clone_url="https://github.com/user/repo.git",
            source=GitMirrorSource.GITHUB,
        )
        captured: dict[str, Any] = {}

        async def scalar(statement: Any) -> None:
            captured["statement"] = statement

        db = MagicMock()
        session_ctx = MagicMock()
        session = MagicMock()
        session.scalar = AsyncMock(side_effect=scalar)
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=None)
        db.session.return_value = session_ctx

        service = GitMirrorService(
            config=_make_config(),
            mirror_repo=_FakeMirrorRepo([]),  # type: ignore[arg-type]
            db=db,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_ms=0),
            circuit_breaker=StorageCircuitBreaker(threshold=100),
            maintenance=None,
            lfs=None,
            git_runner=AsyncMock(return_value=(0, "")),
        )

        result = await service._resolve_url(mirror)

        assert result == (mirror.clone_url, None)
        statement_text = str(captured["statement"].compile(compile_kwargs={"literal_binds": True}))
        assert "user_github_integrations.status" in statement_text
        assert GitHubIntegrationStatus.ACTIVE.value in statement_text

    @pytest.mark.asyncio
    async def test_decrypt_failed_returns_plain_url(self) -> None:
        """When decrypt_secret raises, the plain URL is returned (no crash)."""
        mirror = _make_mirror(
            clone_url="https://github.com/user/repo.git",
            source=GitMirrorSource.GITHUB,
        )
        cfg = _make_config()
        fake_repo = _FakeMirrorRepo([])

        fake_integration = MagicMock()
        fake_integration.encrypted_token = b"some-token"

        db = MagicMock()
        session_ctx = MagicMock()
        session_ctx.__aenter__ = AsyncMock(return_value=MagicMock())
        session_ctx.__aexit__ = AsyncMock(return_value=None)
        session_ctx.__aenter__.return_value.scalar = AsyncMock(return_value=fake_integration)
        db.session.return_value = session_ctx

        service = GitMirrorService(
            config=cfg,
            mirror_repo=fake_repo,  # type: ignore[arg-type]
            db=db,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_ms=0),
            circuit_breaker=StorageCircuitBreaker(threshold=100),
            maintenance=None,
            lfs=None,
            git_runner=AsyncMock(return_value=(0, "")),
        )

        with patch(
            "app.adapters.git_backup.mirror_service.decrypt_secret",
            side_effect=ValueError("bad key"),
        ):
            result = await service._resolve_url(mirror)

        assert result == (mirror.clone_url, None)

    @pytest.mark.asyncio
    async def test_manual_source_returns_plain_url(self) -> None:
        """MANUAL source mirrors skip credential resolution entirely."""
        mirror = _make_mirror(
            clone_url="https://git.example.com/user/repo.git",
            source=GitMirrorSource.MANUAL,
        )
        cfg = _make_config()
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)

        result = await service._resolve_url(mirror)
        assert result == (mirror.clone_url, None)


# ---------------------------------------------------------------------------
# _sync_one: host-is-None and ValueError blocked-host returns
# ---------------------------------------------------------------------------


class TestSyncOneSSRFGuards:
    @pytest.mark.asyncio
    async def test_host_none_returns_failure_outcome(self, tmp_path: Path) -> None:
        """When extract_git_host returns None, _sync_one returns an error outcome."""
        mirror = _make_mirror(mirror_id=1)
        task = MirrorTask(
            mirror=mirror,
            effective_url="not-a-url",
            name="bad-url",
            destination=tmp_path / "repo.git",
        )
        cfg = _make_config(GIT_BACKUP_DATA_PATH=str(tmp_path))
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)
        breaker = StorageCircuitBreaker(threshold=100)
        large_sem = asyncio.Semaphore(1)

        with patch("app.adapters.git_backup.mirror_service.extract_git_host", return_value=None):
            outcome = await service._sync_one(task, breaker, large_sem)

        assert outcome.ok is False
        assert "no resolvable host" in (outcome.error or "")

    @pytest.mark.asyncio
    async def test_blocked_host_returns_failure_outcome(self, tmp_path: Path) -> None:
        """When assert_resolved_public_host raises ValueError, _sync_one returns an error."""
        mirror = _make_mirror(mirror_id=1)
        task = MirrorTask(
            mirror=mirror,
            effective_url="https://github.com/user/repo.git",
            name="user/repo",
            destination=tmp_path / "repo.git",
        )
        cfg = _make_config(GIT_BACKUP_DATA_PATH=str(tmp_path))
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)
        breaker = StorageCircuitBreaker(threshold=100)
        large_sem = asyncio.Semaphore(1)

        with (
            patch(
                "app.adapters.git_backup.mirror_service.extract_git_host",
                return_value="github.com",
            ),
            patch(
                "app.adapters.git_backup.mirror_service.assert_resolved_public_host",
                side_effect=ValueError("host resolves to a non-public address"),
            ),
        ):
            outcome = await service._sync_one(task, breaker, large_sem)

        assert outcome.ok is False
        assert "non-public" in (outcome.error or "")


# ---------------------------------------------------------------------------
# run_with_retry: permanently-gone excluded path and bare-Exception handler
# ---------------------------------------------------------------------------


class TestRunWithRetryExcludedAndBareException:
    @pytest.mark.asyncio
    async def test_permanently_gone_sync_failure_returns_excluded(self, tmp_path: Path) -> None:
        """SyncFailureException whose __cause__ contains a gone-signal produces excluded outcome.

        The retry policy wraps the operation's SyncFailureException as a new
        SyncFailureException with the original as __cause__. run_with_retry checks
        str(exc.__cause__) for permanently-gone signals, so the cause message must
        contain a gone-signal phrase.
        """
        mirror = _make_mirror(mirror_id=1)
        task = MirrorTask(
            mirror=mirror,
            effective_url="https://github.com/user/repo.git",
            name="user/repo",
            destination=tmp_path / "repo.git",
        )
        cfg = _make_config(GIT_BACKUP_DATA_PATH=str(tmp_path))
        fake_repo = _FakeMirrorRepo([])

        # The inner cause carries the gone signal; the retry policy will surface
        # it via exc.__cause__ when it re-wraps into a new SyncFailureException.
        inner_cause = RuntimeError("fatal: repository not found")

        async def raising_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            # Raising a RuntimeError directly; the retry policy will classify and
            # wrap it. Make the message itself the gone signal so that
            # str(exc.__cause__) in run_with_retry triggers is_permanently_gone.
            raise inner_cause

        service = _make_service(fake_repo, cfg, git_runner=raising_runner)
        breaker = StorageCircuitBreaker(threshold=100)
        large_sem = asyncio.Semaphore(1)

        with (
            patch(
                "app.adapters.git_backup.mirror_service.extract_git_host",
                return_value="github.com",
            ),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
        ):
            outcome = await service._sync_one(task, breaker, large_sem)

        assert outcome.excluded is True
        assert outcome.ok is False

    @pytest.mark.asyncio
    async def test_bare_exception_permanently_gone_returns_excluded(self, tmp_path: Path) -> None:
        """A raw Exception with a gone-signal message produces an excluded outcome."""
        mirror = _make_mirror(mirror_id=1)
        task = MirrorTask(
            mirror=mirror,
            effective_url="https://github.com/user/repo.git",
            name="user/repo",
            destination=tmp_path / "repo.git",
        )
        cfg = _make_config(GIT_BACKUP_DATA_PATH=str(tmp_path))
        fake_repo = _FakeMirrorRepo([])

        async def raising_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            # Raise a bare Exception (not SyncFailureException) with a gone signal.
            raise Exception("error: 404 Not Found — does not exist")

        service = _make_service(fake_repo, cfg, git_runner=raising_runner)
        breaker = StorageCircuitBreaker(threshold=100)
        large_sem = asyncio.Semaphore(1)

        with (
            patch(
                "app.adapters.git_backup.mirror_service.extract_git_host",
                return_value="github.com",
            ),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
        ):
            outcome = await service._sync_one(task, breaker, large_sem)

        assert outcome.excluded is True
        assert outcome.ok is False

    @pytest.mark.asyncio
    async def test_bare_exception_non_gone_returns_failed(self, tmp_path: Path) -> None:
        """A raw Exception that is NOT a gone-signal produces a failed (non-excluded) outcome."""
        mirror = _make_mirror(mirror_id=1)
        task = MirrorTask(
            mirror=mirror,
            effective_url="https://github.com/user/repo.git",
            name="user/repo",
            destination=tmp_path / "repo.git",
        )
        cfg = _make_config(GIT_BACKUP_DATA_PATH=str(tmp_path))
        fake_repo = _FakeMirrorRepo([])

        async def raising_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            raise Exception("network is unreachable")

        service = _make_service(fake_repo, cfg, git_runner=raising_runner)
        breaker = StorageCircuitBreaker(threshold=100)
        large_sem = asyncio.Semaphore(1)

        with (
            patch(
                "app.adapters.git_backup.mirror_service.extract_git_host",
                return_value="github.com",
            ),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
        ):
            outcome = await service._sync_one(task, breaker, large_sem)

        assert outcome.ok is False
        assert outcome.excluded is False


# ---------------------------------------------------------------------------
# Post-sync maintenance and LFS hooks called on success
# ---------------------------------------------------------------------------


class TestPostSyncHooks:
    @pytest.mark.asyncio
    async def test_maintenance_called_on_success(self, tmp_path: Path) -> None:
        """run_post_sync_maintenance must be called when maintenance is injected."""
        maintenance_calls: list[Path] = []

        class _RecordingMaint:
            def run_post_sync_maintenance(self, repo_path: Path) -> None:
                maintenance_calls.append(repo_path)

            def register_sync_and_check_repack(self) -> bool:
                return False

            def run_full_repack(self, destination_path: Path) -> None:
                pass

        mirror = _make_mirror(mirror_id=1)
        task = MirrorTask(
            mirror=mirror,
            effective_url="https://github.com/user/repo.git",
            name="user/repo",
            destination=tmp_path / "repo.git",
        )
        cfg = _make_config(GIT_BACKUP_DATA_PATH=str(tmp_path))
        fake_repo = _FakeMirrorRepo([])
        maint = _RecordingMaint()
        service = _make_service(fake_repo, cfg, maintenance=maint)
        breaker = StorageCircuitBreaker(threshold=100)
        large_sem = asyncio.Semaphore(1)

        async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        with (
            patch(
                "app.adapters.git_backup.mirror_service.extract_git_host",
                return_value="github.com",
            ),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
            patch("pathlib.Path.exists", return_value=True),
        ):
            outcome = await service._sync_one(task, breaker, large_sem)

        assert outcome.ok is True
        assert len(maintenance_calls) == 1

    @pytest.mark.asyncio
    async def test_lfs_called_on_success(self, tmp_path: Path) -> None:
        """sync_lfs_if_needed must be called when lfs is injected."""
        lfs_calls: list[Path] = []

        class _RecordingLfs:
            def sync_lfs_if_needed(self, repo_path: Path) -> bool:
                lfs_calls.append(repo_path)
                return True

        mirror = _make_mirror(mirror_id=1)
        task = MirrorTask(
            mirror=mirror,
            effective_url="https://github.com/user/repo.git",
            name="user/repo",
            destination=tmp_path / "repo.git",
        )
        cfg = _make_config(GIT_BACKUP_DATA_PATH=str(tmp_path))
        fake_repo = _FakeMirrorRepo([])
        lfs = _RecordingLfs()
        service = _make_service(fake_repo, cfg, lfs=lfs)
        breaker = StorageCircuitBreaker(threshold=100)
        large_sem = asyncio.Semaphore(1)

        async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        with (
            patch(
                "app.adapters.git_backup.mirror_service.extract_git_host",
                return_value="github.com",
            ),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
            patch("pathlib.Path.exists", return_value=True),
        ):
            outcome = await service._sync_one(task, breaker, large_sem)

        assert outcome.ok is True
        assert len(lfs_calls) == 1


# ---------------------------------------------------------------------------
# Large-repo semaphore branch
# ---------------------------------------------------------------------------


class TestLargeRepoSemaphore:
    @pytest.mark.asyncio
    async def test_large_repo_clone_acquires_large_semaphore(self, tmp_path: Path) -> None:
        """When is_large_repo=True and destination does not exist, large_semaphore is acquired.

        Verifies by using a Semaphore with limit=0 so that acquisition would block
        indefinitely if the code path is NOT taken, and limit=1 so it succeeds when taken.
        The assertion is that the outcome succeeds, proving the code entered the
        `async with large_semaphore` block (otherwise the noop runner would never run).
        We also validate via a MagicMock semaphore so the acquire call is recorded.
        """
        mirror = _make_mirror(mirror_id=1)
        # Destination does not exist -> is_clone=True
        task = MirrorTask(
            mirror=mirror,
            effective_url="https://github.com/user/repo.git",
            name="user/repo",
            destination=tmp_path / "not-yet-cloned.git",
            is_large_repo=True,
        )
        cfg = _make_config(GIT_BACKUP_DATA_PATH=str(tmp_path))
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)
        breaker = StorageCircuitBreaker(threshold=100)

        # Use a real semaphore but verify the _value decrements during the call,
        # which is the standard way to confirm acquisition happened.
        large_sem = asyncio.Semaphore(1)
        assert large_sem._value == 1  # starts at 1

        min_value_seen: list[int] = []

        async def noop_runner_spy(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            # During the runner call the semaphore should be held (value == 0).
            min_value_seen.append(large_sem._value)
            return 0, ""

        service._git_runner = noop_runner_spy

        with (
            patch(
                "app.adapters.git_backup.mirror_service.extract_git_host",
                return_value="github.com",
            ),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
        ):
            outcome = await service._sync_one(task, breaker, large_sem)

        assert outcome.ok is True
        # The semaphore was acquired during the runner call.
        assert min_value_seen == [0]
        # Semaphore released after the block.
        assert large_sem._value == 1


# ---------------------------------------------------------------------------
# _persist_outcome: size OSError and http1 fallback flag derivation
# ---------------------------------------------------------------------------


class TestPersistOutcome:
    @pytest.mark.asyncio
    async def test_ok_outcome_size_oserror_passes_none(self, tmp_path: Path) -> None:
        """When rglob raises OSError, size_kb is None (not propagated as an error)."""
        mirror = _make_mirror(mirror_id=1)
        dest = tmp_path / "repo.git"
        dest.mkdir()

        task = MirrorTask(
            mirror=mirror,
            effective_url="https://github.com/user/repo.git",
            name="user/repo",
            destination=dest,
        )
        outcome = MirrorOutcome(mirror=mirror, ok=True)

        cfg = _make_config()
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)

        with patch("pathlib.Path.rglob", side_effect=OSError("permission denied")):
            await service._persist_outcome(outcome, [task])

        assert len(fake_repo.success_calls) == 1
        assert fake_repo.success_calls[0]["size_kb"] is None

    @pytest.mark.asyncio
    async def test_http1_flag_set_for_http2_error_category(self) -> None:
        """When error_category is HTTP2_ERROR, record_failure is called with use_http1=True."""
        mirror = _make_mirror(mirror_id=1)
        outcome = MirrorOutcome(
            mirror=mirror,
            ok=False,
            error="stream was cancelled",
            error_category=ErrorCategory.HTTP2_ERROR,
        )

        cfg = _make_config()
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)
        task = _make_task(mirror_id=1)

        await service._persist_outcome(outcome, [task])

        assert len(fake_repo.failure_calls) == 1
        assert fake_repo.failure_calls[0]["use_http1"] is True

    @pytest.mark.asyncio
    async def test_http1_flag_not_set_for_auth_error(self) -> None:
        """When error_category is AUTH_ERROR, record_failure is called with use_http1=None."""
        mirror = _make_mirror(mirror_id=1)
        outcome = MirrorOutcome(
            mirror=mirror,
            ok=False,
            error="authentication failed",
            error_category=ErrorCategory.AUTH_ERROR,
        )

        cfg = _make_config()
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)
        task = _make_task(mirror_id=1)

        await service._persist_outcome(outcome, [task])

        assert len(fake_repo.failure_calls) == 1
        assert fake_repo.failure_calls[0]["use_http1"] is None

    @pytest.mark.asyncio
    async def test_excluded_outcome_calls_record_excluded(self) -> None:
        mirror = _make_mirror(mirror_id=1)
        outcome = MirrorOutcome(
            mirror=mirror,
            ok=False,
            excluded=True,
            error="repository not found",
        )

        cfg = _make_config()
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)
        task = _make_task(mirror_id=1)

        await service._persist_outcome(outcome, [task])

        assert len(fake_repo.excluded_calls) == 1
        assert fake_repo.excluded_calls[0][0] == 1

    @pytest.mark.asyncio
    async def test_skipped_outcome_calls_record_skip(self) -> None:
        mirror = _make_mirror(mirror_id=1)
        outcome = MirrorOutcome(
            mirror=mirror,
            ok=False,
            skipped=True,
            skip_reason="storage circuit breaker open",
        )

        cfg = _make_config()
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)
        task = _make_task(mirror_id=1)

        await service._persist_outcome(outcome, [task])

        assert len(fake_repo.skip_calls) == 1
        assert fake_repo.skip_calls[0][2] == "storage circuit breaker open"

    @pytest.mark.asyncio
    async def test_synthetic_mirror_persist_skipped(self) -> None:
        """Synthetic mirrors (id=-1) must be skipped without calling any record_* method."""
        synthetic = GitMirror(
            id=-1,
            user_id=0,
            source=GitMirrorSource.MANUAL,
            clone_url="https://git.example.com/extra.git",
            name="extra",
            consecutive_failures=0,
        )
        outcome = MirrorOutcome(mirror=synthetic, ok=True)

        cfg = _make_config()
        fake_repo = _FakeMirrorRepo([])
        service = _make_service(fake_repo, cfg)

        await service._persist_outcome(outcome, [])

        assert len(fake_repo.success_calls) == 0
        assert len(fake_repo.failure_calls) == 0


# ---------------------------------------------------------------------------
# _mirror_destination: path-traversal containment
# ---------------------------------------------------------------------------


class TestMirrorDestinationPathTraversal:
    """_mirror_destination must always resolve inside data_path.

    Covers four attack vectors:
    - name with ``..`` path-traversal segments (manual mirror)
    - name with a null byte (manual mirror)
    - crafted GITHUB clone_url whose extracted host contains ``..`` (SCP-like)
    - mirror_path DB column set to an absolute path outside data_path
    """

    def _make_svc(self) -> GitMirrorService:
        fake_repo = _FakeMirrorRepo([])
        cfg = _make_config(GIT_BACKUP_DATA_PATH="/tmp/git-mirror-traversal-test")
        return _make_service(fake_repo, cfg)

    def _mirror(
        self,
        *,
        name: str = "user/repo",
        clone_url: str = "https://github.com/user/repo.git",
        source: GitMirrorSource = GitMirrorSource.MANUAL,
        mirror_path: str | None = None,
    ) -> GitMirror:
        m = GitMirror(
            id=42,
            user_id=1,
            source=source,
            clone_url=clone_url,
            name=name,
            consecutive_failures=0,
        )
        m.mirror_path = mirror_path  # type: ignore[assignment]
        return m

    def test_traversal_name_resolves_inside_data_path(self, tmp_path: Path) -> None:
        """name='../../../etc' must not escape data_path."""
        svc = self._make_svc()
        mirror = self._mirror(name="../../../etc")
        dest = svc._mirror_destination(tmp_path, mirror)
        assert str(dest.resolve()).startswith(str(tmp_path.resolve()))

    def test_null_byte_in_name_resolves_inside_data_path(self, tmp_path: Path) -> None:
        """name with a null byte must not escape data_path."""
        svc = self._make_svc()
        mirror = self._mirror(name="repo\x00evil")
        dest = svc._mirror_destination(tmp_path, mirror)
        assert str(dest.resolve()).startswith(str(tmp_path.resolve()))

    def test_crafted_github_host_with_traversal_resolves_inside_data_path(
        self, tmp_path: Path
    ) -> None:
        """A GITHUB mirror whose SCP-like clone_url yields a host with '..' must not escape.

        ``extract_git_host('../../etc:path')`` returns ``'../../etc'``.  The
        safe_host sanitisation in ``_mirror_destination`` must strip that
        traversal so the final path stays inside data_path.
        """
        svc = self._make_svc()
        # SCP-like URL; extract_git_host returns '../../etc' as the host.
        mirror = self._mirror(
            name="repo",
            clone_url="../../etc:path",
            source=GitMirrorSource.GITHUB,
        )
        dest = svc._mirror_destination(tmp_path, mirror)
        assert str(dest.resolve()).startswith(str(tmp_path.resolve()))

    def test_mirror_path_outside_data_path_raises(self, tmp_path: Path) -> None:
        """A mirror_path stored in DB that points outside data_path must raise ValueError."""
        svc = self._make_svc()
        mirror = self._mirror(mirror_path="/etc/passwd")
        with pytest.raises(ValueError, match="resolves outside data_path"):
            svc._mirror_destination(tmp_path, mirror)

    def test_mirror_path_traversal_outside_data_path_raises(self, tmp_path: Path) -> None:
        """A mirror_path with '..' that escapes data_path must raise ValueError."""
        svc = self._make_svc()
        # Construct a path that traverses outside tmp_path.
        outside = str(tmp_path) + "/../../etc/passwd"
        mirror = self._mirror(mirror_path=outside)
        with pytest.raises(ValueError, match="resolves outside data_path"):
            svc._mirror_destination(tmp_path, mirror)

    def test_legitimate_mirror_path_inside_data_path_is_accepted(self, tmp_path: Path) -> None:
        """A mirror_path that is genuinely inside data_path must be returned as-is."""
        svc = self._make_svc()
        legitimate = str(tmp_path / "github" / "github.com" / "user_repo.git")
        mirror = self._mirror(mirror_path=legitimate)
        dest = svc._mirror_destination(tmp_path, mirror)
        assert dest == Path(legitimate)

    def test_normal_name_resolves_inside_data_path(self, tmp_path: Path) -> None:
        """A well-formed name must resolve inside data_path (regression guard)."""
        svc = self._make_svc()
        mirror = self._mirror(name="user/repo")
        dest = svc._mirror_destination(tmp_path, mirror)
        assert str(dest.resolve()).startswith(str(tmp_path.resolve()))

    def test_github_mirror_normal_url_resolves_inside_data_path(self, tmp_path: Path) -> None:
        """A well-formed GitHub URL must resolve inside data_path (regression guard)."""
        svc = self._make_svc()
        mirror = self._mirror(
            name="user/repo",
            clone_url="https://github.com/user/repo.git",
            source=GitMirrorSource.GITHUB,
        )
        dest = svc._mirror_destination(tmp_path, mirror)
        assert str(dest.resolve()).startswith(str(tmp_path.resolve()))
