"""A summary deleted over mobile sync must lose its Qdrant point.

Sync was the one delete entrypoint that only soft-deleted in Postgres. The point
kept its title/tldr/url payload and stayed retrievable through /v1/search and RAG
grounding, and no reconciliation pass pruned it -- so content the user explicitly
deleted was searchable indefinitely.

Both halves are pinned here: the fast path (this file's sync tests) and the
convergence pass that repairs whatever the fast path misses, including the points
that leaked before the fast path existed.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.api.services.sync.apply import SyncApplyService
from app.api.services.sync.serializer import SyncEnvelopeSerializer


class _VectorStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.delete_batches: list[list[int]] = []
        self._fail = fail

    def delete_by_request_ids(self, request_ids: list[int]) -> None:
        if self._fail:
            raise RuntimeError("qdrant unavailable")
        self.delete_batches.append(list(request_ids))


def _summary_repo(*, request_id: int = 4242, server_version: int = 3) -> Any:
    return SimpleNamespace(
        async_get_summary_for_sync_apply=AsyncMock(
            return_value={
                "id": 11,
                "request_id": request_id,
                "server_version": server_version,
                "is_deleted": False,
            }
        ),
        async_apply_sync_change=AsyncMock(return_value=server_version + 1),
    )


def _change(action: str, payload: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        entity_type="summary",
        id="11",
        action=action,
        last_seen_version=3,
        payload=payload,
    )


def _service(repo: Any, vector_store: Any) -> SyncApplyService:
    return SyncApplyService(
        summary_repository=repo,
        serializer=SyncEnvelopeSerializer(),
        vector_store=vector_store,
    )


async def test_sync_delete_removes_the_vector_point() -> None:
    repo = _summary_repo(request_id=4242)
    store = _VectorStore()

    result = await _service(repo, store).apply_change(_change("delete"), user_id=7)

    assert result.status == "applied"
    assert repo.async_apply_sync_change.await_args.kwargs["is_deleted"] is True
    assert store.delete_batches == [[4242]], (
        "sync delete soft-deleted in Postgres without un-indexing; the point keeps "
        "serving the deleted content through search and RAG grounding"
    )


async def test_sync_update_leaves_the_vector_point_alone() -> None:
    """Only a delete un-indexes -- an is_read flip must not drop the vector."""
    repo = _summary_repo()
    store = _VectorStore()

    result = await _service(repo, store).apply_change(
        _change("update", {"is_read": True}), user_id=7
    )

    assert result.status == "applied"
    assert store.delete_batches == []


async def test_sync_delete_survives_a_vector_store_failure() -> None:
    """The user's delete must land in Postgres even when Qdrant is down."""
    repo = _summary_repo()

    result = await _service(repo, _VectorStore(fail=True)).apply_change(
        _change("delete"), user_id=7
    )

    assert result.status == "applied"
    assert repo.async_apply_sync_change.await_args.kwargs["is_deleted"] is True


async def test_sync_delete_without_a_vector_store_still_applies() -> None:
    """Deployments with no vector store configured must not break on delete."""
    repo = _summary_repo()

    result = await SyncApplyService(
        summary_repository=repo, serializer=SyncEnvelopeSerializer()
    ).apply_change(_change("delete"), user_id=7)

    assert result.status == "applied"


class _Db:
    """Minimal stand-in for Database.session() returning fixed prune rows."""

    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self._rows = rows

    def session(self) -> Any:
        rows = self._rows

        @asynccontextmanager
        async def _ctx() -> Any:
            yield SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)))

        return _ctx()


def _prune_runtime(store: Any) -> SimpleNamespace:
    return SimpleNamespace(
        vector_store=store,
        embedding_repository=SimpleNamespace(
            async_mark_summary_embeddings_unindexed=AsyncMock(return_value=[])
        ),
    )


def _prune_module(monkeypatch: pytest.MonkeyPatch, runtime: SimpleNamespace) -> Any:
    """Patch and call through the same module object.

    tests/tasks/test_reconcile_vector_index.py evicts ``app.tasks.*`` from
    sys.modules, so a module-level import here can bind a different module
    instance than a string-path monkeypatch resolves -- the patch then lands on
    an object the code under test never uses. Importing inside the test keeps
    both on one instance regardless of run order.
    """
    from app.tasks import reconcile_vector_index

    monkeypatch.setattr(reconcile_vector_index, "_build_runtime", lambda _cfg, _db: runtime)
    return reconcile_vector_index


async def test_reconciler_prunes_points_of_deleted_summaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The convergence half: points that leaked before the fix get cleaned up."""
    store = _VectorStore()
    runtime = _prune_runtime(store)
    module = _prune_module(monkeypatch, runtime)
    db = _Db(
        [
            SimpleNamespace(summary_id=1, request_id=100),
            SimpleNamespace(summary_id=2, request_id=200),
        ]
    )

    pruned = await module._prune_deleted_summary_vectors(
        cast("Any", None),
        cast("Any", db),
        limit=50,
        correlation_id="cid",
    )

    assert pruned == 2
    assert store.delete_batches == [[100, 200]]
    runtime.embedding_repository.async_mark_summary_embeddings_unindexed.assert_awaited_once_with(
        [1, 2]
    )


async def test_reconciler_keeps_the_marker_when_the_prune_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed Qdrant delete must stay retryable, not be recorded as done."""
    runtime = _prune_runtime(_VectorStore(fail=True))
    module = _prune_module(monkeypatch, runtime)
    db = _Db([SimpleNamespace(summary_id=1, request_id=100)])

    pruned = await module._prune_deleted_summary_vectors(
        cast("Any", None),
        cast("Any", db),
        limit=50,
        correlation_id="cid",
    )

    assert pruned == 0
    runtime.embedding_repository.async_mark_summary_embeddings_unindexed.assert_not_awaited()


async def test_reconciler_prune_is_a_noop_without_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _prune_runtime(_VectorStore())
    module = _prune_module(monkeypatch, runtime)

    pruned = await module._prune_deleted_summary_vectors(
        cast("Any", None), cast("Any", _Db([])), limit=50, correlation_id="cid"
    )

    assert pruned == 0
    runtime.embedding_repository.async_mark_summary_embeddings_unindexed.assert_not_awaited()
