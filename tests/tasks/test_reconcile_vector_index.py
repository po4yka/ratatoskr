"""Tests for app.tasks.reconcile_vector_index."""

from __future__ import annotations

import datetime as dt
import hashlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.observability import metrics as metrics_module


def _stub_taskiq(monkeypatch):
    for mod_name in (
        "taskiq",
        "taskiq.abc",
        "taskiq.abc.schedule_source",
        "taskiq.scheduler",
        "taskiq.scheduler.scheduled_task",
        "taskiq.message",
        "taskiq_redis",
    ):
        if mod_name not in sys.modules:
            monkeypatch.setitem(sys.modules, mod_name, types.ModuleType(mod_name))

    taskiq_mod = sys.modules["taskiq"]
    taskiq_mod.AsyncBroker = object
    taskiq_mod.TaskiqDepends = lambda fn, **_kw: None
    taskiq_mod.TaskiqMiddleware = object
    taskiq_mod.InMemoryBroker = MagicMock
    taskiq_mod.TaskiqScheduler = MagicMock

    msg_mod = sys.modules["taskiq.message"]
    msg_mod.TaskiqMessage = object

    sched_task_mod = sys.modules["taskiq.scheduler.scheduled_task"]
    sched_task_mod.ScheduledTask = MagicMock

    source_mod = sys.modules["taskiq.abc.schedule_source"]
    source_mod.ScheduleSource = object

    tkr_mod = sys.modules["taskiq_redis"]
    tkr_mod.RedisStreamBroker = MagicMock
    tkr_mod.RedisAsyncResultBackend = MagicMock


def _evict_app_tasks() -> None:
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)


def _build_cfg(*, enabled: bool = True, batch_size: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        vector_reconcile=SimpleNamespace(
            enabled=enabled,
            batch_size=batch_size,
            cron="*/30 * * * *",
        ),
        embedding=SimpleNamespace(max_token_length=512),
        vector_store=SimpleNamespace(environment="test", user_scope="owner"),
    )


def _counter_value(counter, **labels) -> float:
    if counter is None:
        return 0.0
    sample = counter.labels(**labels)
    return float(sample._value.get())


def _gauge_value(gauge) -> float:
    if gauge is None:
        return 0.0
    return float(gauge._value.get())


@pytest.mark.asyncio
async def test_reconcile_short_circuits_when_disabled(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.reconcile_vector_index import _reconcile_body

    fetch_spy = AsyncMock()
    monkeypatch.setattr(
        "app.tasks.reconcile_vector_index._fetch_stale_summaries",
        fetch_spy,
    )

    summary = await _reconcile_body(_build_cfg(enabled=False), MagicMock())

    assert summary.scanned == 0
    assert summary.requeued == 0
    fetch_spy.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_returns_zero_when_no_stale_rows(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.reconcile_vector_index import _reconcile_body

    monkeypatch.setattr(
        "app.tasks.reconcile_vector_index._fetch_stale_summaries",
        AsyncMock(return_value=[]),
    )

    summary = await _reconcile_body(_build_cfg(), MagicMock())

    assert summary == summary.__class__(scanned=0, requeued=0, skipped=0, failed=0)


@pytest.mark.asyncio
async def test_reconcile_batches_stale_rows_with_force_true(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.application.services.summary_embedding_generator import EmbeddingBatchResult
    from app.tasks.reconcile_vector_index import _reconcile_body

    rows = [
        {"summary_id": 11, "json_payload": {"summary_250": "a"}, "lang_detected": "en"},
        {"summary_id": 22, "json_payload": {"summary_250": "b"}, "lang_detected": "ru"},
        # Non-dict payload — the generator counts it as skipped, not failed.
        {"summary_id": 33, "json_payload": "legacy-string", "lang_detected": None},
    ]
    monkeypatch.setattr(
        "app.tasks.reconcile_vector_index._fetch_stale_summaries",
        AsyncMock(return_value=rows),
    )

    fake_generator = SimpleNamespace(
        generate_embeddings_for_summaries=AsyncMock(
            return_value=EmbeddingBatchResult(indexed=1, skipped=2, failed=0)
        )
    )
    monkeypatch.setattr(
        "app.tasks.reconcile_vector_index._build_runtime",
        lambda _cfg, _db: SimpleNamespace(embedding_generator=fake_generator),
    )
    monkeypatch.setattr(
        "app.tasks.reconcile_vector_index._sync_summary_vectors",
        AsyncMock(return_value=1),
    )

    summary = await _reconcile_body(_build_cfg(), MagicMock())

    assert summary.scanned == 3
    assert summary.requeued == 1
    assert summary.skipped == 2
    assert summary.failed == 0

    # All rows are handed to the batch method in one call, with force=True.
    call = fake_generator.generate_embeddings_for_summaries.await_args
    items = call.args[0]
    assert [it[0] for it in items] == [11, 22, 33]
    assert [it[2] for it in items] == ["en", "ru", None]
    assert call.kwargs["force"] is True


@pytest.mark.asyncio
async def test_reconcile_surfaces_batch_failure_counts(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.application.services.summary_embedding_generator import EmbeddingBatchResult
    from app.tasks.reconcile_vector_index import _reconcile_body

    monkeypatch.setattr(
        "app.tasks.reconcile_vector_index._fetch_stale_summaries",
        AsyncMock(
            return_value=[
                {"summary_id": 1, "json_payload": {"x": 1}, "lang_detected": None},
                {"summary_id": 2, "json_payload": {"x": 2}, "lang_detected": None},
            ]
        ),
    )

    fake_generator = SimpleNamespace(
        generate_embeddings_for_summaries=AsyncMock(
            return_value=EmbeddingBatchResult(indexed=1, skipped=0, failed=1)
        )
    )
    monkeypatch.setattr(
        "app.tasks.reconcile_vector_index._build_runtime",
        lambda _cfg, _db: SimpleNamespace(embedding_generator=fake_generator),
    )
    monkeypatch.setattr(
        "app.tasks.reconcile_vector_index._sync_summary_vectors",
        AsyncMock(return_value=1),
    )

    summary = await _reconcile_body(_build_cfg(), MagicMock())

    assert summary.scanned == 2
    assert summary.requeued == 1
    assert summary.failed == 1
    assert summary.skipped == 0


@pytest.mark.asyncio
async def test_reconcile_records_metrics_for_forced_run(monkeypatch):
    if metrics_module.VECTOR_RECONCILE_ROWS_TOTAL is None:
        pytest.skip("prometheus_client is not available")
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.application.services.summary_embedding_generator import EmbeddingBatchResult
    from app.tasks.reconcile_vector_index import _reconcile_body

    now = dt.datetime.now(dt.UTC)
    rows = [
        {
            "summary_id": 1,
            "json_payload": {"summary_250": "a"},
            "lang_detected": "en",
            "updated_at": now - dt.timedelta(minutes=15),
            "last_indexed_at": now - dt.timedelta(hours=2),
        },
        {
            "summary_id": 2,
            "json_payload": {"summary_250": "b"},
            "lang_detected": "ru",
            "updated_at": now - dt.timedelta(minutes=20),
            "last_indexed_at": None,
        },
        {
            "summary_id": 3,
            "json_payload": {"summary_250": "c"},
            "lang_detected": None,
            "updated_at": now - dt.timedelta(minutes=10),
            "last_indexed_at": now - dt.timedelta(minutes=30),
        },
    ]
    monkeypatch.setattr(
        "app.tasks.reconcile_vector_index._fetch_stale_summaries",
        AsyncMock(return_value=rows),
    )

    fake_generator = SimpleNamespace(
        generate_embeddings_for_summaries=AsyncMock(
            return_value=EmbeddingBatchResult(indexed=1, skipped=1, failed=1)
        )
    )
    monkeypatch.setattr(
        "app.tasks.reconcile_vector_index._build_runtime",
        lambda _cfg, _db: SimpleNamespace(embedding_generator=fake_generator),
    )
    monkeypatch.setattr(
        "app.tasks.reconcile_vector_index._sync_summary_vectors",
        AsyncMock(return_value=1),
    )

    before_scanned = _counter_value(metrics_module.VECTOR_RECONCILE_ROWS_TOTAL, outcome="scanned")
    before_requeued = _counter_value(metrics_module.VECTOR_RECONCILE_ROWS_TOTAL, outcome="requeued")
    before_skipped = _counter_value(metrics_module.VECTOR_RECONCILE_ROWS_TOTAL, outcome="skipped")
    before_failed = _counter_value(metrics_module.VECTOR_RECONCILE_ROWS_TOTAL, outcome="failed")
    before_success = _counter_value(metrics_module.VECTOR_RECONCILE_RUNS_TOTAL, status="success")

    summary = await _reconcile_body(_build_cfg(), MagicMock())

    assert summary.scanned == 3
    assert summary.requeued == 1
    assert summary.skipped == 1
    assert summary.failed == 1
    assert (
        _counter_value(metrics_module.VECTOR_RECONCILE_ROWS_TOTAL, outcome="scanned")
        == before_scanned + 3
    )
    assert (
        _counter_value(metrics_module.VECTOR_RECONCILE_ROWS_TOTAL, outcome="requeued")
        == before_requeued + 1
    )
    assert (
        _counter_value(metrics_module.VECTOR_RECONCILE_ROWS_TOTAL, outcome="skipped")
        == before_skipped + 1
    )
    assert (
        _counter_value(metrics_module.VECTOR_RECONCILE_ROWS_TOTAL, outcome="failed")
        == before_failed + 1
    )
    assert (
        _counter_value(metrics_module.VECTOR_RECONCILE_RUNS_TOTAL, status="success")
        == before_success + 1
    )
    assert _gauge_value(metrics_module.VECTOR_RECONCILE_OLDEST_LAG_SECONDS) >= 7200


@pytest.mark.asyncio
async def test_sync_summary_vectors_upserts_qdrant_and_marks_indexed(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.reconcile_vector_index import _sync_summary_vectors

    class FakeVectorStore:
        available = True

        def __init__(self):
            self.replaced = []

        def replace_summary_point(self, request_id, raw_id, vector, payload):
            self.replaced.append((request_id, raw_id, vector, payload))
            return True

    vector_store = FakeVectorStore()
    embedding_repo = SimpleNamespace(
        async_get_summary_embeddings=AsyncMock(
            return_value=[
                {
                    "summary_id": 11,
                    "embedding_blob": b"blob",
                    "content_hash": hashlib.sha256(b"summary ai").hexdigest(),
                }
            ]
        ),
        async_mark_summary_embeddings_indexed=AsyncMock(),
    )
    embedding_service = SimpleNamespace(deserialize_embedding=lambda _blob: [0.5, 0.6])
    runtime = SimpleNamespace(
        vector_store=vector_store,
        embedding_repository=embedding_repo,
        embedding_generator=SimpleNamespace(embedding_service=embedding_service),
    )

    indexed = await _sync_summary_vectors(
        _build_cfg(),
        runtime,
        [
            {
                "summary_id": 11,
                "request_id": 22,
                "json_payload": {"summary_250": "summary", "topic_tags": ["ai"]},
                "lang": "en",
                "lang_detected": None,
            }
        ],
    )

    assert indexed == 1
    assert vector_store.replaced == [
        (
            22,
            "22:11",
            [0.5, 0.6],
            {
                "entity_type": "summary",
                "summary_id": 11,
                "request_id": 22,
                "language": "en",
                "user_scope": "owner",
                "environment": "test",
                "title": "",
                "url": "",
                "source_type": "",
                "tldr": "",
                "topic_tags": ["ai"],
                "summary_250": "summary",
            },
        )
    ]
    embedding_repo.async_mark_summary_embeddings_indexed.assert_awaited_once_with([11])
