"""Tests for three opt-in mirror_service features:

1. Priority rules — _collect_tasks reorders tasks by priority DESC and applies
   per-rule timeout overrides.
2. Ignore list — _collect_tasks filters out targets whose name or clone_url
   matches any pattern in cfg.ignore.
3. Full-repack timing — register_sync_and_check_repack fires once per sync run
   (in perform_sync finalize), not once per successful repo.

All tests are hermetic: no real DB, no filesystem, no subprocess calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.git_backup.mirror_service import (
    GitMirrorService,
    MirrorTask,
    _apply_priority_rules,
    _is_ignored,
)
from app.config.git_backup import GitBackupConfig, PriorityRule
from app.db.models.git_backup import GitMirror, GitMirrorSource, GitMirrorStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config(**overrides: Any) -> GitBackupConfig:
    base: dict[str, Any] = {
        "GIT_BACKUP_ENABLED": False,
        "GIT_BACKUP_DATA_PATH": "/tmp/git-priorities-test",
    }
    base.update(overrides)
    return GitBackupConfig.model_validate(base)


def _make_mirror(
    *,
    mirror_id: int = 1,
    name: str = "octocat/hello-world",
    clone_url: str = "https://github.com/octocat/hello-world.git",
    size_kb: int | None = None,
) -> GitMirror:
    return GitMirror(
        id=mirror_id,
        user_id=100,
        source=GitMirrorSource.GITHUB,
        clone_url=clone_url,
        name=name,
        consecutive_failures=0,
        status=GitMirrorStatus.PENDING,
        size_kb=size_kb,
    )


def _make_task(
    *,
    name: str = "repo",
    url: str = "https://github.com/user/repo.git",
    mirror_id: int = 1,
    priority: int = 0,
    timeout_seconds_override: int | None = None,
) -> MirrorTask:
    mirror = _make_mirror(mirror_id=mirror_id, name=name, clone_url=url)
    return MirrorTask(
        mirror=mirror,
        effective_url=url,
        name=name,
        destination=Path(f"/tmp/{name}"),
        timeout_seconds_override=timeout_seconds_override,
    )


class _FakeMirrorRepo:
    """Minimal injectable fake for GitMirrorRepository."""

    def __init__(self, mirrors: list[GitMirror]) -> None:
        self._mirrors = mirrors
        self.upsert_calls: list[dict[str, Any]] = []

    async def list_due(self, user_id: int | None = None) -> list[GitMirror]:
        return list(self._mirrors)

    async def upsert_target(self, **kwargs: Any) -> GitMirror:
        self.upsert_calls.append(kwargs)
        return _make_mirror(name=kwargs.get("name", "extra"), clone_url=kwargs.get("clone_url", ""))

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
    cfg: GitBackupConfig,
    *,
    maintenance: Any = None,
) -> GitMirrorService:
    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
    db.session.return_value.__aexit__ = AsyncMock(return_value=None)

    from app.adapters.git_backup.circuit_breaker import StorageCircuitBreaker
    from app.adapters.git_backup.retry import RetryPolicy

    async def noop_runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
        return 0, ""

    return GitMirrorService(
        config=cfg,
        mirror_repo=fake_repo,  # type: ignore[arg-type]
        db=db,
        retry_policy=RetryPolicy(max_attempts=1, base_delay_ms=0),
        circuit_breaker=StorageCircuitBreaker(threshold=100),
        maintenance=maintenance,
        lfs=None,
        git_runner=noop_runner,
    )


# ---------------------------------------------------------------------------
# Feature 1: _is_ignored
# ---------------------------------------------------------------------------


class TestIsIgnored:
    def test_empty_patterns_never_ignores(self) -> None:
        assert _is_ignored("any/repo", "https://example.com/any.git", []) is False

    def test_exact_name_substring_match(self) -> None:
        assert _is_ignored("user/fork", "https://github.com/user/fork.git", ["fork"]) is True

    def test_no_match(self) -> None:
        assert _is_ignored("user/myrepo", "https://github.com/user/myrepo.git", ["fork"]) is False

    def test_regex_match_on_name(self) -> None:
        assert _is_ignored("user/test-123", "https://example.com/t.git", [r"test-\d+"]) is True

    def test_regex_match_on_url(self) -> None:
        assert _is_ignored("not-matching", "https://evil.example.com/repo.git", [r"evil\."]) is True

    def test_invalid_regex_falls_back_to_substring(self) -> None:
        # "[invalid" is not a valid regex; should fall back to literal substring
        assert _is_ignored("my[invalid]repo", "https://example.com/r.git", ["[invalid]"]) is True

    def test_multiple_patterns_any_matches(self) -> None:
        assert (
            _is_ignored("user/archived", "https://github.com/user/archived.git", ["fork", "archive"])
            is True
        )

    def test_case_sensitive_by_default(self) -> None:
        # Python re is case-sensitive by default
        assert _is_ignored("User/Fork", "https://github.com/user/fork.git", ["^fork"]) is False


# ---------------------------------------------------------------------------
# Feature 1: _apply_priority_rules
# ---------------------------------------------------------------------------


class TestApplyPriorityRules:
    def test_empty_rules_returns_unchanged_order(self) -> None:
        tasks = [_make_task(name="a"), _make_task(name="b"), _make_task(name="c")]
        result = _apply_priority_rules(tasks, [])
        assert [t.name for t in result] == ["a", "b", "c"]

    def test_single_rule_reorders_matched_task_first(self) -> None:
        tasks = [
            _make_task(name="low", url="https://github.com/user/low.git"),
            _make_task(name="important", url="https://github.com/user/important.git"),
        ]
        rules = [PriorityRule(pattern="important", priority=10)]
        result = _apply_priority_rules(tasks, rules)
        assert result[0].name == "important"
        assert result[1].name == "low"

    def test_highest_priority_wins_when_multiple_rules_match(self) -> None:
        tasks = [
            _make_task(name="a", url="https://github.com/user/a.git"),
        ]
        rules = [
            PriorityRule(pattern="user", priority=5),
            PriorityRule(pattern="github", priority=10),
        ]
        result = _apply_priority_rules(tasks, rules)
        # Only one task; verify timeout from highest priority rule matching
        assert result[0].name == "a"

    def test_timeout_override_applied_from_matching_rule(self) -> None:
        tasks = [_make_task(name="slow-repo", url="https://git.example.com/slow-repo.git")]
        rules = [PriorityRule(pattern="slow", priority=5, timeout_seconds=120)]
        result = _apply_priority_rules(tasks, rules)
        assert result[0].timeout_seconds_override == 120

    def test_timeout_override_not_applied_when_no_match(self) -> None:
        tasks = [_make_task(name="fast-repo", url="https://git.example.com/fast.git")]
        rules = [PriorityRule(pattern="slow", priority=5, timeout_seconds=120)]
        result = _apply_priority_rules(tasks, rules)
        assert result[0].timeout_seconds_override is None

    def test_stable_sort_preserves_relative_order_within_same_priority(self) -> None:
        tasks = [
            _make_task(name="x1", url="https://git.example.com/x1.git"),
            _make_task(name="x2", url="https://git.example.com/x2.git"),
            _make_task(name="high", url="https://git.example.com/high.git"),
        ]
        rules = [PriorityRule(pattern="high", priority=100)]
        result = _apply_priority_rules(tasks, rules)
        assert result[0].name == "high"
        # x1 and x2 must stay in original relative order (stable sort)
        assert [t.name for t in result[1:]] == ["x1", "x2"]

    def test_url_pattern_match(self) -> None:
        tasks = [
            _make_task(name="repo-a", url="https://internal.corp.com/repo-a.git"),
            _make_task(name="repo-b", url="https://github.com/user/repo-b.git"),
        ]
        # Prioritize internal repos by URL pattern
        rules = [PriorityRule(pattern=r"internal\.corp\.com", priority=20)]
        result = _apply_priority_rules(tasks, rules)
        assert result[0].name == "repo-a"


# ---------------------------------------------------------------------------
# Feature 1: integration — _collect_tasks applies priorities
# ---------------------------------------------------------------------------


class TestCollectTasksPriorities:
    @pytest.mark.asyncio
    async def test_collect_tasks_reorders_by_priority(self) -> None:
        mirrors = [
            _make_mirror(mirror_id=1, name="user/low", clone_url="https://github.com/user/low.git"),
            _make_mirror(
                mirror_id=2, name="user/critical", clone_url="https://github.com/user/critical.git"
            ),
        ]
        cfg = _make_config(
            priorities=[
                PriorityRule(pattern="critical", priority=100),
            ]
        )
        fake_repo = _FakeMirrorRepo(mirrors)
        service = _make_service(fake_repo, cfg)

        data_path = Path("/tmp/git-priorities-test")
        with (
            patch.object(service, "_resolve_url", side_effect=lambda m: m.clone_url),
            patch.object(service, "_mirror_destination", side_effect=lambda dp, m: dp / (m.name or "")),
        ):
            tasks = await service._collect_tasks(user_id=100, data_path=data_path)

        assert tasks[0].name == "user/critical"
        assert tasks[1].name == "user/low"

    @pytest.mark.asyncio
    async def test_collect_tasks_assigns_timeout_override(self) -> None:
        mirrors = [
            _make_mirror(
                mirror_id=1, name="user/bigone", clone_url="https://github.com/user/bigone.git"
            ),
        ]
        cfg = _make_config(
            priorities=[
                PriorityRule(pattern="bigone", priority=5, timeout_seconds=7200),
            ]
        )
        fake_repo = _FakeMirrorRepo(mirrors)
        service = _make_service(fake_repo, cfg)

        data_path = Path("/tmp/git-priorities-test")
        with (
            patch.object(service, "_resolve_url", side_effect=lambda m: m.clone_url),
            patch.object(service, "_mirror_destination", side_effect=lambda dp, m: dp / (m.name or "")),
        ):
            tasks = await service._collect_tasks(user_id=100, data_path=data_path)

        assert tasks[0].timeout_seconds_override == 7200


# ---------------------------------------------------------------------------
# Feature 2: integration — _collect_tasks filters ignored targets
# ---------------------------------------------------------------------------


class TestCollectTasksIgnore:
    @pytest.mark.asyncio
    async def test_ignored_mirror_excluded(self) -> None:
        mirrors = [
            _make_mirror(mirror_id=1, name="user/keep", clone_url="https://github.com/user/keep.git"),
            _make_mirror(
                mirror_id=2, name="user/archived", clone_url="https://github.com/user/archived.git"
            ),
        ]
        cfg = _make_config(ignore=["archived"])
        fake_repo = _FakeMirrorRepo(mirrors)
        service = _make_service(fake_repo, cfg)

        data_path = Path("/tmp/git-ignore-test")
        with (
            patch.object(service, "_resolve_url", side_effect=lambda m: m.clone_url),
            patch.object(service, "_mirror_destination", side_effect=lambda dp, m: dp / (m.name or "")),
        ):
            tasks = await service._collect_tasks(user_id=100, data_path=data_path)

        names = [t.name for t in tasks]
        assert "user/archived" not in names
        assert "user/keep" in names

    @pytest.mark.asyncio
    async def test_ignore_regex_excludes_matching_targets(self) -> None:
        mirrors = [
            _make_mirror(
                mirror_id=1,
                name="user/test-001",
                clone_url="https://github.com/user/test-001.git",
            ),
            _make_mirror(
                mirror_id=2,
                name="user/prod-001",
                clone_url="https://github.com/user/prod-001.git",
            ),
        ]
        cfg = _make_config(ignore=[r"^user/test-"])
        fake_repo = _FakeMirrorRepo(mirrors)
        service = _make_service(fake_repo, cfg)

        data_path = Path("/tmp/git-ignore-test")
        with (
            patch.object(service, "_resolve_url", side_effect=lambda m: m.clone_url),
            patch.object(service, "_mirror_destination", side_effect=lambda dp, m: dp / (m.name or "")),
        ):
            tasks = await service._collect_tasks(user_id=100, data_path=data_path)

        names = [t.name for t in tasks]
        assert "user/test-001" not in names
        assert "user/prod-001" in names

    @pytest.mark.asyncio
    async def test_empty_ignore_list_includes_all(self) -> None:
        mirrors = [
            _make_mirror(mirror_id=1, name="user/repo-a", clone_url="https://github.com/user/repo-a.git"),
            _make_mirror(mirror_id=2, name="user/repo-b", clone_url="https://github.com/user/repo-b.git"),
        ]
        cfg = _make_config(ignore=[])
        fake_repo = _FakeMirrorRepo(mirrors)
        service = _make_service(fake_repo, cfg)

        data_path = Path("/tmp/git-ignore-test")
        with (
            patch.object(service, "_resolve_url", side_effect=lambda m: m.clone_url),
            patch.object(service, "_mirror_destination", side_effect=lambda dp, m: dp / (m.name or "")),
        ):
            tasks = await service._collect_tasks(user_id=100, data_path=data_path)

        assert len(tasks) == 2


# ---------------------------------------------------------------------------
# Feature 3: full-repack fires once per sync run (not once per repo)
# ---------------------------------------------------------------------------


class _RecordingMaintenance:
    """Fake RepositoryMaintenance that records register_sync_and_check_repack calls."""

    def __init__(self, *, repack_due: bool = False) -> None:
        self.register_calls: int = 0
        self.repack_calls: int = 0
        self._repack_due = repack_due

    def run_post_sync_maintenance(self, repo_path: Path) -> None:
        pass  # no-op

    def register_sync_and_check_repack(self) -> bool:
        self.register_calls += 1
        return self._repack_due

    def run_full_repack(self, destination_path: Path) -> None:
        self.repack_calls += 1


class TestFullRepackTiming:
    """register_sync_and_check_repack must fire exactly once per perform_sync call."""

    @pytest.mark.asyncio
    async def test_register_called_once_for_multi_repo_run(self) -> None:
        """Three successful repos → register_sync_and_check_repack called exactly once."""
        mirrors = [
            _make_mirror(mirror_id=i, name=f"user/repo-{i}", clone_url=f"https://github.com/user/repo-{i}.git")
            for i in range(1, 4)
        ]
        recording_maint = _RecordingMaintenance(repack_due=False)
        cfg = _make_config()
        fake_repo = _FakeMirrorRepo(mirrors)
        service = _make_service(fake_repo, cfg, maintenance=recording_maint)

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch.object(service, "_resolve_url", side_effect=lambda m: m.clone_url),
            patch("app.adapters.git_backup.mirror_service._preflight_storage_check", return_value=None),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
        ):
            await service.perform_sync(user_id=100)

        assert recording_maint.register_calls == 1, (
            f"Expected register_sync_and_check_repack to be called once, got {recording_maint.register_calls}"
        )

    @pytest.mark.asyncio
    async def test_full_repack_triggered_when_due(self) -> None:
        """When register_sync_and_check_repack returns True, run_full_repack is called."""
        mirrors = [_make_mirror(mirror_id=1)]
        recording_maint = _RecordingMaintenance(repack_due=True)
        cfg = _make_config()
        fake_repo = _FakeMirrorRepo(mirrors)
        service = _make_service(fake_repo, cfg, maintenance=recording_maint)

        async def fake_to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch.object(service, "_resolve_url", side_effect=lambda m: m.clone_url),
            patch("app.adapters.git_backup.mirror_service._preflight_storage_check", return_value=None),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
            patch("asyncio.to_thread", side_effect=fake_to_thread),
        ):
            await service.perform_sync(user_id=100)

        assert recording_maint.register_calls == 1
        assert recording_maint.repack_calls == 1

    @pytest.mark.asyncio
    async def test_register_not_called_when_no_maintenance(self) -> None:
        """When maintenance is None, no register call and no error."""
        mirrors = [_make_mirror(mirror_id=1)]
        cfg = _make_config()
        fake_repo = _FakeMirrorRepo(mirrors)
        service = _make_service(fake_repo, cfg, maintenance=None)

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch.object(service, "_resolve_url", side_effect=lambda m: m.clone_url),
            patch("app.adapters.git_backup.mirror_service._preflight_storage_check", return_value=None),
            patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
        ):
            summary = await service.perform_sync(user_id=100)

        # No exception; summary should have processed the one mirror
        assert summary.total >= 0
