"""Tests for on-disk cleanup on DELETE and the stale-EXCLUDED prune sweep.

Coverage:
- Path-safety check: mirror_path outside data_path → rmtree NOT called.
- Path-safety check: mirror_path inside data_path → rmtree IS called.
- GitMirrorRepository.list_stale_excluded: only returns old EXCLUDED rows.
- Prune sweep (_prune_stale_excluded): deletes Qdrant point + dir + DB row
  for each stale mirror; skips mirrors with unsafe paths.

All tests are hermetic: no real DB, no filesystem I/O (except via tmp_path),
no subprocess calls.
"""

from __future__ import annotations

import datetime as dt
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from app.config.git_backup import GitBackupConfig
from app.db.models.git_backup import GitMirror, GitMirrorSource, GitMirrorStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mirror(
    *,
    mirror_id: int = 1,
    status: GitMirrorStatus = GitMirrorStatus.EXCLUDED,
    excluded_at: dt.datetime | None = None,
    mirror_path: str | None = None,
) -> GitMirror:
    m = GitMirror(
        id=mirror_id,
        user_id=100,
        source=GitMirrorSource.MANUAL,
        clone_url="https://example.com/repo.git",
        name="repo",
        consecutive_failures=0,
        status=status,
    )
    m.excluded_at = excluded_at
    m.mirror_path = mirror_path
    return m


def _make_config(data_path: str = "/data/git-mirrors", **overrides: Any) -> GitBackupConfig:
    base: dict[str, Any] = {
        "GIT_BACKUP_ENABLED": False,
        "GIT_BACKUP_DATA_PATH": data_path,
    }
    base.update(overrides)
    return GitBackupConfig.model_validate(base)


# ---------------------------------------------------------------------------
# Path-safety check for DELETE endpoint logic
# ---------------------------------------------------------------------------


class TestPathSafetyCheck:
    """The rmtree gate must accept paths inside data_path and reject those outside."""

    def test_inside_data_path_is_relative(self, tmp_path: Path) -> None:
        data_root = tmp_path / "git-mirrors"
        mirror_dir = data_root / "manual" / "repo.git"
        assert mirror_dir.resolve().is_relative_to(data_root.resolve())

    def test_outside_data_path_not_relative(self, tmp_path: Path) -> None:
        data_root = tmp_path / "git-mirrors"
        outside = tmp_path / "other" / "evil.git"
        assert not outside.resolve().is_relative_to(data_root.resolve())

    def test_parent_traversal_rejected(self, tmp_path: Path) -> None:
        data_root = tmp_path / "git-mirrors"
        # Resolving "git-mirrors/../other" lands outside data_root.
        traversal = (data_root / ".." / "escape").resolve()
        assert not traversal.is_relative_to(data_root.resolve())

    def test_rmtree_called_for_safe_path(self, tmp_path: Path) -> None:
        """When mirror_path is inside data_path, rmtree must be invoked."""
        data_root = tmp_path / "git-mirrors"
        mirror_dir = data_root / "manual" / "repo.git"
        mirror_dir.mkdir(parents=True)

        # Simulate the guard logic used in the DELETE endpoint.
        target = mirror_dir.resolve()
        root = data_root.resolve()
        rmtree_called = False

        if target.is_relative_to(root):
            shutil.rmtree(target)
            rmtree_called = True

        assert rmtree_called
        assert not mirror_dir.exists()

    def test_rmtree_not_called_for_unsafe_path(self, tmp_path: Path) -> None:
        """When mirror_path escapes data_path, rmtree must NOT be invoked."""
        data_root = tmp_path / "git-mirrors"
        outside = tmp_path / "outside" / "repo.git"
        outside.mkdir(parents=True)

        target = outside.resolve()
        root = data_root.resolve()
        rmtree_called = False

        if target.is_relative_to(root):
            shutil.rmtree(target)
            rmtree_called = True

        assert not rmtree_called
        assert outside.exists()  # untouched


# ---------------------------------------------------------------------------
# GitMirrorRepository.list_stale_excluded
# ---------------------------------------------------------------------------


class FakeScalarsResult:
    def __init__(self, rows: list[GitMirror]) -> None:
        self._rows = rows

    def all(self) -> list[GitMirror]:
        return self._rows


class FakeSessionForListStale:
    """Session fake that applies the list_stale_excluded WHERE clauses manually."""

    def __init__(self, rows: list[GitMirror], cutoff: dt.datetime) -> None:
        self._rows = rows
        self._cutoff = cutoff

    async def scalars(self, stmt: Any) -> FakeScalarsResult:
        eligible = [
            r
            for r in self._rows
            if r.status == GitMirrorStatus.EXCLUDED
            and r.excluded_at is not None
            and r.excluded_at < self._cutoff
        ]
        return FakeScalarsResult(eligible)


class FakeSessionCtxForListStale:
    def __init__(self, rows: list[GitMirror], cutoff: dt.datetime) -> None:
        self._session = FakeSessionForListStale(rows, cutoff)

    async def __aenter__(self) -> FakeSessionForListStale:
        return self._session

    async def __aexit__(self, *args: Any) -> None:
        pass


class FakeDBForListStale:
    def __init__(self, rows: list[GitMirror], cutoff: dt.datetime) -> None:
        self._rows = rows
        self._cutoff = cutoff

    def session(self) -> FakeSessionCtxForListStale:
        return FakeSessionCtxForListStale(self._rows, self._cutoff)


class TestListStaleExcluded:
    async def test_returns_old_excluded_only(self) -> None:
        from app.adapters.git_backup.repository import GitMirrorRepository

        now = dt.datetime.now(tz=dt.UTC)
        old_excluded = _make_mirror(
            mirror_id=1,
            status=GitMirrorStatus.EXCLUDED,
            excluded_at=now - dt.timedelta(days=40),
        )
        recent_excluded = _make_mirror(
            mirror_id=2,
            status=GitMirrorStatus.EXCLUDED,
            excluded_at=now - dt.timedelta(days=5),
        )
        ok_mirror = _make_mirror(mirror_id=3, status=GitMirrorStatus.OK)

        days = 30
        cutoff = now - dt.timedelta(days=days)
        db = FakeDBForListStale([old_excluded, recent_excluded, ok_mirror], cutoff)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        result = await repo.list_stale_excluded(days)

        ids = {r.id for r in result}
        assert ids == {1}, "Only the mirror excluded >30 days ago should be returned"

    async def test_returns_empty_when_none_stale(self) -> None:
        from app.adapters.git_backup.repository import GitMirrorRepository

        now = dt.datetime.now(tz=dt.UTC)
        recent_excluded = _make_mirror(
            mirror_id=1,
            status=GitMirrorStatus.EXCLUDED,
            excluded_at=now - dt.timedelta(days=5),
        )
        days = 30
        cutoff = now - dt.timedelta(days=days)
        db = FakeDBForListStale([recent_excluded], cutoff)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        result = await repo.list_stale_excluded(days)
        assert result == []

    async def test_excluded_without_excluded_at_not_returned(self) -> None:
        from app.adapters.git_backup.repository import GitMirrorRepository

        # An EXCLUDED row with excluded_at=None (should never happen in prod but
        # the filter must not select it).
        no_ts = _make_mirror(mirror_id=1, status=GitMirrorStatus.EXCLUDED, excluded_at=None)
        days = 30
        now = dt.datetime.now(tz=dt.UTC)
        cutoff = now - dt.timedelta(days=days)
        db = FakeDBForListStale([no_ts], cutoff)
        cfg = _make_config()
        repo = GitMirrorRepository(db, cfg)  # type: ignore[arg-type]

        result = await repo.list_stale_excluded(days)
        assert result == []


# ---------------------------------------------------------------------------
# Prune sweep (_prune_stale_excluded)
# ---------------------------------------------------------------------------


class FakeMirrorRepoForPrune:
    """Injectable fake for GitMirrorRepository used in prune sweep tests."""

    def __init__(self, stale_mirrors: list[GitMirror]) -> None:
        self._stale = stale_mirrors
        self.deleted_ids: list[int] = []

    async def list_stale_excluded(self, older_than_days: int) -> list[GitMirror]:
        return list(self._stale)

    async def delete_mirror(self, mirror_id: int) -> None:
        self.deleted_ids.append(mirror_id)


def _make_fake_mirror_repo_cls(fake_repo: FakeMirrorRepoForPrune) -> Any:
    """Return a class whose constructor ignores its args and returns fake_repo."""

    class _FakeCls:
        def __new__(cls, *args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
            return fake_repo

    return _FakeCls


class TestPruneSweep:
    """_prune_stale_excluded deletes Qdrant point + dir + DB row for stale mirrors."""

    async def test_prune_deletes_point_and_row(self, tmp_path: Path) -> None:
        """When mirror_path is inside data_path, rmtree and delete_mirror are called."""
        from app.tasks.git_backup_sync import _prune_stale_excluded

        data_root = tmp_path / "git-mirrors"
        mirror_dir = data_root / "manual" / "repo.git"
        mirror_dir.mkdir(parents=True)

        now = dt.datetime.now(tz=dt.UTC)
        stale = _make_mirror(
            mirror_id=7,
            status=GitMirrorStatus.EXCLUDED,
            excluded_at=now - dt.timedelta(days=40),
            mirror_path=str(mirror_dir),
        )

        fake_repo = FakeMirrorRepoForPrune([stale])
        fake_qdrant = MagicMock()
        fake_qdrant.available = True
        fake_qdrant.delete_git_mirror_points = MagicMock()

        cfg_obj = _make_config(
            data_path=str(data_root),
            GIT_BACKUP_PRUNE_EXCLUDED_DAYS=30,
        )
        fake_app_cfg = MagicMock()
        fake_app_cfg.git_backup = cfg_obj

        # _prune_stale_excluded does `from app.adapters.git_backup.repository import
        # GitMirrorRepository` and `from app.di.shared import build_qdrant_vector_store`
        # as local imports.  Patch at the source locations so the local `from … import`
        # picks up the patched name.
        with (
            patch(
                "app.adapters.git_backup.repository.GitMirrorRepository",
                new=_make_fake_mirror_repo_cls(fake_repo),
            ),
            patch(
                "app.di.shared.build_qdrant_vector_store",
                return_value=fake_qdrant,
            ),
        ):
            await _prune_stale_excluded(fake_app_cfg, MagicMock())

        assert 7 in fake_repo.deleted_ids
        fake_qdrant.delete_git_mirror_points.assert_called_once_with([7])
        assert not mirror_dir.exists()

    async def test_prune_skips_unsafe_path(self, tmp_path: Path) -> None:
        """When mirror_path is outside data_path, rmtree is NOT called but row IS deleted."""
        from app.tasks.git_backup_sync import _prune_stale_excluded

        data_root = tmp_path / "git-mirrors"
        outside_dir = tmp_path / "outside" / "evil.git"
        outside_dir.mkdir(parents=True)

        now = dt.datetime.now(tz=dt.UTC)
        stale = _make_mirror(
            mirror_id=8,
            status=GitMirrorStatus.EXCLUDED,
            excluded_at=now - dt.timedelta(days=40),
            mirror_path=str(outside_dir),
        )

        fake_repo = FakeMirrorRepoForPrune([stale])
        fake_qdrant = MagicMock()
        fake_qdrant.available = True
        fake_qdrant.delete_git_mirror_points = MagicMock()

        cfg_obj = _make_config(
            data_path=str(data_root),
            GIT_BACKUP_PRUNE_EXCLUDED_DAYS=30,
        )
        fake_app_cfg = MagicMock()
        fake_app_cfg.git_backup = cfg_obj

        with (
            patch(
                "app.adapters.git_backup.repository.GitMirrorRepository",
                new=_make_fake_mirror_repo_cls(fake_repo),
            ),
            patch(
                "app.di.shared.build_qdrant_vector_store",
                return_value=fake_qdrant,
            ),
        ):
            await _prune_stale_excluded(fake_app_cfg, MagicMock())

        # DB row IS deleted (unsafe path does not block row deletion).
        assert 8 in fake_repo.deleted_ids
        # On-disk dir is NOT touched.
        assert outside_dir.exists()

    async def test_prune_noop_when_disabled(self) -> None:
        """When prune_excluded_days == 0, the sweep must return without doing anything."""
        from app.tasks.git_backup_sync import _prune_stale_excluded

        fake_repo = FakeMirrorRepoForPrune([])
        cfg_obj = _make_config(GIT_BACKUP_PRUNE_EXCLUDED_DAYS=0)
        fake_app_cfg = MagicMock()
        fake_app_cfg.git_backup = cfg_obj

        # prune_excluded_days==0 returns immediately before any repo construction.
        with patch(
            "app.adapters.git_backup.repository.GitMirrorRepository",
            new=_make_fake_mirror_repo_cls(fake_repo),
        ):
            await _prune_stale_excluded(fake_app_cfg, MagicMock())

        assert fake_repo.deleted_ids == []

    async def test_prune_no_stale_mirrors(self) -> None:
        """When list_stale_excluded returns empty, no deletions happen."""
        from app.tasks.git_backup_sync import _prune_stale_excluded

        fake_repo = FakeMirrorRepoForPrune([])  # empty
        fake_qdrant = MagicMock()
        fake_qdrant.available = True
        fake_qdrant.delete_git_mirror_points = MagicMock()

        cfg_obj = _make_config(GIT_BACKUP_PRUNE_EXCLUDED_DAYS=30)
        fake_app_cfg = MagicMock()
        fake_app_cfg.git_backup = cfg_obj

        with (
            patch(
                "app.adapters.git_backup.repository.GitMirrorRepository",
                new=_make_fake_mirror_repo_cls(fake_repo),
            ),
            patch(
                "app.di.shared.build_qdrant_vector_store",
                return_value=fake_qdrant,
            ),
        ):
            await _prune_stale_excluded(fake_app_cfg, MagicMock())

        assert fake_repo.deleted_ids == []
        fake_qdrant.delete_git_mirror_points.assert_not_called()
