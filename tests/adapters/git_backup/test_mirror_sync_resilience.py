"""Failure isolation for a git-mirror sync run.

Every case here is a defect where one bad mirror cost the whole run: an
exception escaping gather skipped the persist loop, the circuit breaker was
consulted before the worker slot so it could never trip mid-run, a single
failing outcome write stranded the rest, an unusable DB row aborted collection,
and a killed clone poisoned every retry that followed.

Shares the fakes with test_mirror_service_coverage so neither file owns the
other's scaffolding.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from app.adapters.git_backup.circuit_breaker import StorageCircuitBreaker
from app.adapters.git_backup.errors import ErrorCategory
from app.adapters.git_backup.mirror_service import GitMirrorService, MirrorOutcome, MirrorTask

from .test_mirror_service_coverage import (
    _FakeMirrorRepo,
    _make_config,
    _make_mirror,
    _make_service,
)

# ---------------------------------------------------------------------------
# Failure isolation across a sync run
# ---------------------------------------------------------------------------


def _hermetic(service: GitMirrorService):
    """Neutralise storage preflight, credential resolution and DNS for a run."""
    return _ExitStackCM(
        patch.object(service, "_resolve_url", side_effect=lambda m: (m.clone_url, None)),
        patch(
            "app.adapters.git_backup.mirror_service._preflight_storage_check",
            AsyncMock(return_value=None),
        ),
        patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"),
    )


class _ExitStackCM:
    def __init__(self, *cms: Any) -> None:
        self._cms = cms
        self._stack = contextlib.ExitStack()

    def __enter__(self) -> None:
        for cm in self._cms:
            self._stack.enter_context(cm)

    def __exit__(self, *exc: Any) -> None:
        self._stack.close()


class TestSyncRunFailureIsolation:
    """One bad mirror must not cost the run its outcomes."""

    @pytest.mark.asyncio
    async def test_unexpected_error_in_one_mirror_still_persists_the_others(self) -> None:
        """gather used to propagate, skipping the persist loop entirely.

        Mirrors that had already cloned never got record_success or
        last_synced_at, the failed one never incremented consecutive_failures so
        its cooldown never engaged, and sibling coroutines kept cloning after the
        Redis lock had been released.
        """
        mirrors = [
            _make_mirror(mirror_id=1, name="a/one"),
            _make_mirror(mirror_id=2, name="b/boom"),
            _make_mirror(mirror_id=3, name="c/three"),
        ]
        repo = _FakeMirrorRepo(mirrors)
        cfg = _make_config(GIT_BACKUP_WORKERS=3)
        service = _make_service(repo, cfg)

        async def _sync_one(task, breaker, large_semaphore):  # type: ignore[no-untyped-def]
            if task.mirror.id == 2:
                # e.g. ENOSPC from the destination mkdir
                raise OSError("[Errno 28] No space left on device")
            return MirrorOutcome(mirror=task.mirror, ok=True)

        with _hermetic(service), patch.object(service, "_sync_one", _sync_one):
            summary = await service.perform_sync(user_id=100)

        assert summary.ok == 2
        assert summary.failed == 1
        # The two healthy mirrors were recorded despite the sibling blowing up.
        assert sorted(c["mirror_id"] for c in repo.success_calls) == [1, 3]
        # And the failure was recorded rather than lost, so the cooldown engages.
        assert [c["mirror_id"] for c in repo.failure_calls] == [2]

    @pytest.mark.asyncio
    async def test_one_failing_persist_does_not_strand_the_rest(self) -> None:
        """The persist loop is the only place last_synced_at is written."""
        mirrors = [_make_mirror(mirror_id=i, name=f"m/{i}") for i in (1, 2, 3)]
        repo = _FakeMirrorRepo(mirrors)
        service = _make_service(repo, _make_config(GIT_BACKUP_WORKERS=3))

        async def _sync_one(task, breaker, large_semaphore):  # type: ignore[no-untyped-def]
            return MirrorOutcome(mirror=task.mirror, ok=True)

        real_persist = service._persist_outcome
        calls: list[int] = []

        async def _persist(outcome, tasks):  # type: ignore[no-untyped-def]
            calls.append(outcome.mirror.id)
            if outcome.mirror.id == 1:
                raise RuntimeError("db write failed")
            await real_persist(outcome, tasks)

        with (
            _hermetic(service),
            patch.object(service, "_sync_one", _sync_one),
            patch.object(service, "_persist_outcome", _persist),
        ):
            await service.perform_sync(user_id=100)

        assert calls == [1, 2, 3], "the loop stopped at the first failing write"
        assert sorted(c["mirror_id"] for c in repo.success_calls) == [2, 3]

    @pytest.mark.asyncio
    async def test_breaker_tripped_mid_run_skips_the_remaining_mirrors(self) -> None:
        """The breaker check has to happen after the worker slot is acquired.

        gather schedules every run_one at once, so a check placed before the
        semaphore was evaluated by all of them in one pass -- before any mirror
        could record a failure. With the breaker also rebuilt per run, the
        documented "abort the remainder of the sync run" never fired: a full disk
        tripped it after three mirrors while the rest kept cloning into it.
        """
        mirrors = [_make_mirror(mirror_id=i, name=f"m/{i}") for i in range(1, 6)]
        repo = _FakeMirrorRepo(mirrors)
        breaker = StorageCircuitBreaker(threshold=1)
        # One worker so the tasks are strictly serialised.
        service = _make_service(repo, _make_config(GIT_BACKUP_WORKERS=1), circuit_breaker=breaker)

        attempted: list[int] = []

        async def _sync_one(task, brk, large_semaphore):  # type: ignore[no-untyped-def]
            attempted.append(task.mirror.id)
            # Yield once before recording, exactly as a real clone does at its
            # first await. Without this the fake completes inside its first
            # scheduling step, the tasks never interleave, and the test cannot
            # tell a pre-semaphore check from a post-semaphore one.
            await asyncio.sleep(0)
            brk.record_failure(ErrorCategory.STORAGE_ERROR)
            return MirrorOutcome(
                mirror=task.mirror,
                ok=False,
                error="no space left on device",
                error_category=ErrorCategory.STORAGE_ERROR,
            )

        with _hermetic(service), patch.object(service, "_sync_one", _sync_one):
            summary = await service.perform_sync(user_id=100)

        assert attempted == [1], f"kept cloning after the breaker opened: {attempted}"
        assert summary.skipped == 4
        assert breaker.is_open() is True

    @pytest.mark.asyncio
    async def test_unusable_mirror_row_is_skipped_not_fatal(self) -> None:
        """_mirror_destination raises for every pre-existing row after a data-path change."""
        mirrors = [
            _make_mirror(mirror_id=1, name="ok/one"),
            _make_mirror(mirror_id=2, name="bad/two"),
        ]
        repo = _FakeMirrorRepo(mirrors)
        service = _make_service(repo, _make_config(GIT_BACKUP_WORKERS=2))

        real_destination = service._mirror_destination

        def _destination(data_path, mirror):  # type: ignore[no-untyped-def]
            if mirror.id == 2:
                raise ValueError("mirror destination resolves outside data_path")
            return real_destination(data_path, mirror)

        async def _sync_one(task, breaker, large_semaphore):  # type: ignore[no-untyped-def]
            return MirrorOutcome(mirror=task.mirror, ok=True)

        with (
            _hermetic(service),
            patch.object(service, "_mirror_destination", _destination),
            patch.object(service, "_sync_one", _sync_one),
        ):
            summary = await service.perform_sync(user_id=100)

        # The healthy row still ran instead of the run dying during collection.
        assert summary.ok == 1
        assert [c["mirror_id"] for c in repo.success_calls] == [1]


class TestPartialCloneRetry:
    """A killed clone leaves a destination that is not yet a repository."""

    @staticmethod
    def _task(dest: Path) -> MirrorTask:
        return MirrorTask(
            mirror=_make_mirror(name="user/repo"),
            effective_url="https://github.com/user/repo.git",
            name="user/repo",
            destination=dest,
        )

    @pytest.mark.asyncio
    async def test_leftover_without_head_is_reset_and_recloned(self, tmp_path: Path) -> None:
        """is_clone was computed once and captured by the retry closure.

        After a clone that was killed or timed out, `dest` exists but has no
        HEAD, so every later attempt still built a *clone* command and git
        refused with "destination path already exists" -- burning the whole
        retry budget and recording UNKNOWN in place of the real TIMEOUT.
        """
        dest = tmp_path / "mirrors" / "user" / "repo.git"
        dest.mkdir(parents=True)
        (dest / "half-written.pack").write_text("garbage")

        seen: list[list[str]] = []

        async def _runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            seen.append(argv)
            return 0, ""

        cfg = _make_config(GIT_BACKUP_DATA_PATH=str(tmp_path))
        service = _make_service(_FakeMirrorRepo(), cfg, git_runner=_runner)

        with patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"):
            outcome = await service._sync_one(
                self._task(dest), StorageCircuitBreaker(threshold=10), asyncio.Semaphore(1)
            )

        assert outcome.ok is True
        # The stale directory was cleared, so git had an empty target.
        assert not (dest / "half-written.pack").exists()
        # And the command really was a clone: the URL is in the argv.
        assert any("https://github.com/user/repo.git" in tok for tok in seen[0])

    @pytest.mark.asyncio
    async def test_real_mirror_is_updated_not_recloned(self, tmp_path: Path) -> None:
        """A destination carrying HEAD is a finished mirror; leave it alone."""
        dest = tmp_path / "mirrors" / "user" / "repo.git"
        dest.mkdir(parents=True)
        (dest / "HEAD").write_text("ref: refs/heads/main\n")
        (dest / "keep.pack").write_text("real data")

        seen: list[list[str]] = []

        async def _runner(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
            seen.append(argv)
            return 0, ""

        cfg = _make_config(GIT_BACKUP_DATA_PATH=str(tmp_path))
        service = _make_service(_FakeMirrorRepo(), cfg, git_runner=_runner)

        with patch("app.adapters.git_backup.mirror_service.assert_resolved_public_host"):
            outcome = await service._sync_one(
                self._task(dest), StorageCircuitBreaker(threshold=10), asyncio.Semaphore(1)
            )

        assert outcome.ok is True
        assert (dest / "keep.pack").exists(), "an existing mirror was destroyed"
        assert not any("https://github.com/user/repo.git" in tok for tok in seen[0])
