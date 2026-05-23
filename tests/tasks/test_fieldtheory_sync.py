"""Tests for app.tasks.fieldtheory_sync (Taskiq bookmark delta-scan)."""

from __future__ import annotations

import sqlite3
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


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


def _build_cfg(
    *,
    enabled: bool = True,
    bookmarks_db_path: str = "/fieldtheory/bookmarks.db",
) -> SimpleNamespace:
    return SimpleNamespace(
        fieldtheory=SimpleNamespace(
            enabled=enabled,
            sync_cron="*/15 * * * *",
            bookmarks_db_path=bookmarks_db_path,
        ),
    )


def _build_stats(**overrides):
    from app.adapters.ingestors.fieldtheory_ingestor import FieldTheoryIngestStats

    return FieldTheoryIngestStats(**overrides)


@pytest.mark.asyncio
async def test_sync_short_circuits_when_disabled(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.fieldtheory_sync import _sync_body

    runtime_spy = MagicMock()
    monkeypatch.setattr(
        "app.tasks.fieldtheory_sync.build_fieldtheory_task_runtime",
        runtime_spy,
    )

    summary = await _sync_body(_build_cfg(enabled=False), MagicMock())

    assert summary.bookmarks_seen == 0
    assert summary.requests_created == 0
    assert summary.metadata_inserted == 0
    assert summary.metadata_updated == 0
    runtime_spy.assert_not_called()


@pytest.mark.asyncio
async def test_sync_returns_ingestor_stats_on_happy_path(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.fieldtheory_sync import _sync_body

    ingestor = SimpleNamespace(
        sync=AsyncMock(
            return_value=_build_stats(
                bookmarks_seen=7,
                requests_created=3,
                metadata_inserted=3,
                metadata_updated=2,
                skipped_invalid_category=1,
                skipped_invalid_url=1,
            )
        ),
    )
    monkeypatch.setattr(
        "app.tasks.fieldtheory_sync.build_fieldtheory_task_runtime",
        lambda cfg, db: SimpleNamespace(cfg=cfg, db=db, ingestor=ingestor),
    )

    summary = await _sync_body(_build_cfg(), MagicMock())

    ingestor.sync.assert_awaited_once()
    assert summary.bookmarks_seen == 7
    assert summary.requests_created == 3
    assert summary.metadata_inserted == 3
    assert summary.metadata_updated == 2
    assert summary.skipped_invalid_category == 1
    assert summary.skipped_invalid_url == 1


@pytest.mark.asyncio
async def test_sync_swallows_sqlite_operational_error(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.fieldtheory_sync import _sync_body

    ingestor = SimpleNamespace(
        sync=AsyncMock(side_effect=sqlite3.OperationalError("unable to open database file"))
    )
    monkeypatch.setattr(
        "app.tasks.fieldtheory_sync.build_fieldtheory_task_runtime",
        lambda cfg, db: SimpleNamespace(cfg=cfg, db=db, ingestor=ingestor),
    )

    summary = await _sync_body(_build_cfg(), MagicMock())

    assert summary.bookmarks_seen == 0
    assert summary.requests_created == 0
    ingestor.sync.assert_awaited_once()


@pytest.mark.asyncio
async def test_sync_propagates_unexpected_exception(monkeypatch):
    """Generic errors (not sqlite3.OperationalError) must surface to Taskiq retry."""
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.fieldtheory_sync import _sync_body

    ingestor = SimpleNamespace(sync=AsyncMock(side_effect=RuntimeError("unexpected")))
    monkeypatch.setattr(
        "app.tasks.fieldtheory_sync.build_fieldtheory_task_runtime",
        lambda cfg, db: SimpleNamespace(cfg=cfg, db=db, ingestor=ingestor),
    )

    with pytest.raises(RuntimeError, match="unexpected"):
        await _sync_body(_build_cfg(), MagicMock())


def test_di_build_fieldtheory_task_runtime_constructs_ingestor(monkeypatch) -> None:
    """The DI factory wires the ingestor with the configured bookmarks path."""
    _stub_taskiq(monkeypatch)
    _evict_app_tasks()

    from app.di.tasks import build_fieldtheory_task_runtime

    cfg = SimpleNamespace(
        fieldtheory=SimpleNamespace(
            enabled=True,
            sync_cron="*/15 * * * *",
            bookmarks_db_path="/tmp/test-bookmarks.db",
        ),
    )
    db = SimpleNamespace(name="db")

    runtime = build_fieldtheory_task_runtime(cfg, db)  # type: ignore[arg-type]

    assert runtime.cfg is cfg
    assert runtime.db is db
    assert runtime.ingestor is not None
    # Path is normalized to pathlib.Path inside the ingestor.
    assert str(runtime.ingestor._bookmarks_db_path) == "/tmp/test-bookmarks.db"
