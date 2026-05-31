"""Targeted hermetic tests for uncovered branches in git_mirror_reconciler.

Covers the three gaps left by the existing suite:

1. lines 120-122: ``_delete_orphans`` except-handler — Qdrant raises, returns 0.
2. branch 134->129 / lines 138-139: ``_repair_missing`` path-not-found branch
   when mirror_path is None (no path string at all).
3. lines 140-141: per-item except-handler in ``_repair_missing`` loop — indexer
   raises, item is skipped, loop continues.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Fakes — mirror the idiom from tests/adapters/git_backup/test_git_mirror_reconciler.py
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, session: Any) -> None:
        self._s = session

    async def __aenter__(self) -> Any:
        return self._s

    async def __aexit__(self, *_args: Any) -> bool:
        return False


class _ReconSession:
    def __init__(self, rows: list[Any], mirrors: dict[int, Any], log: list[Any]) -> None:
        self._rows = rows
        self._mirrors = mirrors
        self._log = log

    async def execute(self, stmt: Any) -> Any:
        self._log.append(stmt)
        res = MagicMock()
        res.all.return_value = self._rows
        return res

    async def get(self, _model: Any, id_: int) -> Any:
        return self._mirrors.get(id_)


class _ReconDb:
    def __init__(self, rows: list[Any], mirrors: dict[int, Any]) -> None:
        self._rows = rows
        self._mirrors = mirrors
        self.exec_log: list[Any] = []

    def session(self) -> _Ctx:
        return _Ctx(_ReconSession(self._rows, self._mirrors, self.exec_log))

    def transaction(self) -> _Ctx:
        return _Ctx(_ReconSession(self._rows, self._mirrors, self.exec_log))


class _ReconQdrant:
    """Fake Qdrant store; optionally raises on delete."""

    def __init__(
        self,
        indexed: set[int],
        available: bool = True,
        delete_raises: bool = False,
    ) -> None:
        self.available = available
        self._indexed = indexed
        self.deleted: list[list[int]] = []
        self._delete_raises = delete_raises

    def get_indexed_git_mirror_ids(self, *, limit: int | None = None) -> set[int]:
        return set(self._indexed)

    def delete_git_mirror_points(self, ids: Any) -> None:
        if self._delete_raises:
            raise RuntimeError("qdrant connection lost")
        self.deleted.append(list(ids))


class _ReconIndexer:
    """Fake indexer; optionally raises on a specified mirror_id."""

    def __init__(self, raise_on: set[int] | None = None) -> None:
        self.calls: list[tuple[Any, Path, bool]] = []
        self._raise_on: set[int] = raise_on or set()

    async def index_mirror(self, mirror: Any, path: Path, *, force: bool = False) -> None:
        if mirror.id in self._raise_on:
            raise OSError(f"disk error for mirror {mirror.id}")
        self.calls.append((mirror, path, force))


def _mirror(mirror_id: int = 1) -> MagicMock:
    m = MagicMock()
    m.id = mirror_id
    return m


def _build_reconciler(db: Any, qdrant: Any, indexer: Any) -> Any:
    from app.infrastructure.search.git_mirror_reconciler import GitMirrorVectorReconciler

    return GitMirrorVectorReconciler(db=db, qdrant_store=qdrant, indexer=indexer)


# ---------------------------------------------------------------------------
# 1. _delete_orphans except-handler (lines 120-122)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_orphans_returns_zero_on_qdrant_error() -> None:
    """When Qdrant.delete_git_mirror_points raises, _delete_orphans catches the
    exception, logs it, and returns 0 (best-effort; does not propagate)."""
    # rows: only mirror 1 is expected; mirror 99 is an orphan in Qdrant.
    rows = [SimpleNamespace(id=1, mirror_path="/some/path")]
    db = _ReconDb(rows, {1: _mirror(mirror_id=1)})
    qdrant = _ReconQdrant(indexed={1, 99}, delete_raises=True)
    indexer = _ReconIndexer()
    rec = _build_reconciler(db, qdrant, indexer)

    report = await rec.reconcile_and_repair()

    # Despite the Qdrant failure, the reconciler completes and reports 0 deleted.
    assert report.orphans_deleted == 0
    # The failure did not propagate — no exception raised to the caller.
    assert report.missing_reindexed == 0
    assert report.missing_cleared == 0


@pytest.mark.asyncio
async def test_delete_orphans_returns_zero_on_qdrant_error_isolate() -> None:
    """Direct call to _delete_orphans confirms it returns 0 on exception."""
    qdrant = _ReconQdrant(indexed=set(), delete_raises=True)
    db = _ReconDb([], {})
    indexer = _ReconIndexer()
    rec = _build_reconciler(db, qdrant, indexer)

    result = await rec._delete_orphans([10, 20])

    assert result == 0


# ---------------------------------------------------------------------------
# 2. _repair_missing path-not-found branch when mirror_path is None (lines 138-139)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repair_missing_clears_when_mirror_path_is_none() -> None:
    """When expected_paths[mirror_id] is None (no path stored), the clear-index
    path executes: _clear_index_state is called and cleared count increments."""
    # DB says mirror 3 has readme_indexed_at set but mirror_path is NULL.
    rows = [SimpleNamespace(id=3, mirror_path=None)]
    db = _ReconDb(rows, {3: _mirror(mirror_id=3)})
    # Qdrant has no vector for mirror 3 -> it is in the missing set.
    qdrant = _ReconQdrant(indexed=set())
    indexer = _ReconIndexer()
    rec = _build_reconciler(db, qdrant, indexer)

    report = await rec.reconcile_and_repair()

    # The path-not-found branch ran: cleared += 1, no re-index attempt.
    assert report.missing_cleared == 1
    assert report.missing_reindexed == 0
    assert indexer.calls == []
    # An UPDATE clearing readme_indexed_at was executed.
    assert any("UPDATE" in str(stmt).upper() for stmt in db.exec_log)


@pytest.mark.asyncio
async def test_repair_missing_clears_isolate_none_path() -> None:
    """Direct call to _repair_missing with a None-path entry."""
    rows: list[Any] = []
    db = _ReconDb(rows, {})
    qdrant = _ReconQdrant(indexed=set())
    indexer = _ReconIndexer()
    rec = _build_reconciler(db, qdrant, indexer)

    reindexed, cleared = await rec._repair_missing([7], {7: None})

    assert reindexed == 0
    assert cleared == 1


@pytest.mark.asyncio
async def test_repair_missing_clears_isolate_empty_string_path() -> None:
    """An empty string path is falsy, so the clear branch also runs."""
    rows: list[Any] = []
    db = _ReconDb(rows, {})
    qdrant = _ReconQdrant(indexed=set())
    indexer = _ReconIndexer()
    rec = _build_reconciler(db, qdrant, indexer)

    reindexed, cleared = await rec._repair_missing([8], {8: ""})

    assert reindexed == 0
    assert cleared == 1


# ---------------------------------------------------------------------------
# 3. Per-item except-handler in _repair_missing loop (lines 140-141)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repair_missing_skips_failed_item_continues_loop(tmp_path: Path) -> None:
    """If index_mirror raises for one item, the except-handler logs and skips it;
    subsequent items in the loop still process correctly."""
    path_good = str(tmp_path)

    # Two missing mirrors: mirror 11 will raise, mirror 12 will succeed.
    rows = [
        SimpleNamespace(id=11, mirror_path=path_good),
        SimpleNamespace(id=12, mirror_path=path_good),
    ]
    mirror11 = _mirror(mirror_id=11)
    mirror12 = _mirror(mirror_id=12)
    db = _ReconDb(rows, {11: mirror11, 12: mirror12})
    qdrant = _ReconQdrant(indexed=set())  # both missing
    indexer = _ReconIndexer(raise_on={11})
    rec = _build_reconciler(db, qdrant, indexer)

    report = await rec.reconcile_and_repair()

    # Mirror 11 failed silently; mirror 12 succeeded.
    assert report.missing_reindexed == 1
    assert report.missing_cleared == 0
    assert len(indexer.calls) == 1
    called_mirror, _called_path, force = indexer.calls[0]
    assert called_mirror is mirror12
    assert force is True


@pytest.mark.asyncio
async def test_repair_missing_all_items_fail_returns_zero(tmp_path: Path) -> None:
    """All items raising -> reindexed and cleared both remain 0."""
    rows = [SimpleNamespace(id=5, mirror_path=str(tmp_path))]
    db = _ReconDb(rows, {5: _mirror(mirror_id=5)})
    qdrant = _ReconQdrant(indexed=set())
    indexer = _ReconIndexer(raise_on={5})
    rec = _build_reconciler(db, qdrant, indexer)

    report = await rec.reconcile_and_repair()

    assert report.missing_reindexed == 0
    assert report.missing_cleared == 0


@pytest.mark.asyncio
async def test_repair_missing_exception_does_not_propagate(tmp_path: Path) -> None:
    """The per-item except block must swallow exceptions; reconcile_and_repair
    must still return a complete report object."""
    rows = [SimpleNamespace(id=99, mirror_path=str(tmp_path))]
    db = _ReconDb(rows, {99: _mirror(mirror_id=99)})
    qdrant = _ReconQdrant(indexed=set())
    indexer = _ReconIndexer(raise_on={99})
    rec = _build_reconciler(db, qdrant, indexer)

    # Must not raise.
    from app.infrastructure.search.git_mirror_reconciler import GitMirrorRepairReport

    report = await rec.reconcile_and_repair()
    assert isinstance(report, GitMirrorRepairReport)
