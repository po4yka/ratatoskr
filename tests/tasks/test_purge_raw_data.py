"""Tests for app.tasks.purge_raw_data."""

from __future__ import annotations

import datetime as dt
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
    enabled=True,
    batch_size=100,
    telegram_raw_days=7,
    crawl_content_days=7,
    llm_payload_days=7,
    video_transcript_days=7,
    downloaded_media_days=7,
    interaction_text_days=7,
    request_content_days=7,
    export_temp_file_max_age_seconds=3600,
    privacy_no_retention_mode=False,
):
    return SimpleNamespace(
        retention=SimpleNamespace(
            enabled=enabled,
            batch_size=batch_size,
            privacy_no_retention_mode=privacy_no_retention_mode,
            telegram_raw_days=telegram_raw_days,
            crawl_content_days=crawl_content_days,
            llm_payload_days=llm_payload_days,
            video_transcript_days=video_transcript_days,
            downloaded_media_days=downloaded_media_days,
            interaction_text_days=interaction_text_days,
            request_content_days=request_content_days,
            export_temp_file_max_age_seconds=export_temp_file_max_age_seconds,
        )
    )


def _make_mock_db(rowcount=3):
    """Return mock Database whose transaction context yields rowcount on execute."""
    mock_db = MagicMock()
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.rowcount = rowcount
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_db.transaction.return_value.__aenter__ = AsyncMock(return_value=mock_session)
    mock_db.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
    return mock_db


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_body_disabled_returns_zero_stats(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import PurgeStats, _purge_body

    mock_db = _make_mock_db(rowcount=5)
    result = await _purge_body(_build_cfg(enabled=False), mock_db)

    assert result == PurgeStats()
    mock_db.transaction.assert_not_called()


@pytest.mark.asyncio
async def test_null_columns_ttl_zero_skips_db(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import _purge_telegram_raw

    mock_db = _make_mock_db(rowcount=5)
    now = dt.datetime.now(dt.UTC)

    result = await _purge_telegram_raw(mock_db, now, days=0, batch=100)

    assert result == 0
    mock_db.transaction.assert_not_called()


@pytest.mark.asyncio
async def test_purge_telegram_raw_returns_rowcount(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import _purge_telegram_raw

    mock_db = _make_mock_db(rowcount=5)
    now = dt.datetime.now(dt.UTC)

    result = await _purge_telegram_raw(mock_db, now, days=7, batch=100)

    assert result == 5
    mock_db.transaction.return_value.__aenter__.assert_called_once()
    session = await mock_db.transaction.return_value.__aenter__()
    session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_purge_crawl_content_returns_rowcount(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import _purge_crawl_content

    mock_db = _make_mock_db(rowcount=3)
    now = dt.datetime.now(dt.UTC)

    result = await _purge_crawl_content(mock_db, now, days=7, batch=100)

    assert result == 3
    session = await mock_db.transaction.return_value.__aenter__()
    session.execute.assert_called()


@pytest.mark.asyncio
async def test_purge_llm_payload_returns_rowcount(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import _purge_llm_payload

    mock_db = _make_mock_db(rowcount=12)
    now = dt.datetime.now(dt.UTC)

    result = await _purge_llm_payload(mock_db, now, days=7, batch=100)

    assert result == 12
    session = await mock_db.transaction.return_value.__aenter__()
    session.execute.assert_called()


@pytest.mark.asyncio
async def test_purge_video_transcript_returns_rowcount(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import _purge_video_transcript

    mock_db = _make_mock_db(rowcount=2)
    now = dt.datetime.now(dt.UTC)

    result = await _purge_video_transcript(mock_db, now, days=7, batch=100)

    assert result == 2
    session = await mock_db.transaction.return_value.__aenter__()
    session.execute.assert_called()


@pytest.mark.asyncio
async def test_purge_interaction_text_returns_rowcount(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import _purge_interaction_text

    mock_db = _make_mock_db(rowcount=7)
    now = dt.datetime.now(dt.UTC)

    result = await _purge_interaction_text(mock_db, now, days=7, batch=100)

    assert result == 7
    session = await mock_db.transaction.return_value.__aenter__()
    session.execute.assert_called()


@pytest.mark.asyncio
async def test_purge_request_content_returns_rowcount(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import _purge_request_content

    mock_db = _make_mock_db(rowcount=4)
    now = dt.datetime.now(dt.UTC)

    result = await _purge_request_content(mock_db, now, days=7, batch=100)

    assert result == 4
    session = await mock_db.transaction.return_value.__aenter__()
    session.execute.assert_called()


@pytest.mark.asyncio
async def test_purge_idempotent_zero_rowcount(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import _purge_crawl_content

    mock_db = _make_mock_db(rowcount=0)
    now = dt.datetime.now(dt.UTC)

    result = await _purge_crawl_content(mock_db, now, days=7, batch=100)

    assert result == 0
    session = await mock_db.transaction.return_value.__aenter__()
    session.execute.assert_called_once()


@pytest.mark.asyncio
async def test_purge_body_aggregates_subsystem_counts(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import PurgeStats, _purge_body

    monkeypatch.setattr(
        "app.tasks.purge_raw_data._purge_telegram_raw",
        AsyncMock(return_value=1),
    )
    monkeypatch.setattr(
        "app.tasks.purge_raw_data._purge_crawl_content",
        AsyncMock(return_value=2),
    )
    monkeypatch.setattr(
        "app.tasks.purge_raw_data._purge_llm_payload",
        AsyncMock(return_value=3),
    )
    monkeypatch.setattr(
        "app.tasks.purge_raw_data._purge_video_transcript",
        AsyncMock(return_value=4),
    )
    monkeypatch.setattr(
        "app.tasks.purge_raw_data._purge_downloaded_media",
        AsyncMock(return_value=7),
    )
    monkeypatch.setattr(
        "app.tasks.purge_raw_data._purge_interaction_text",
        AsyncMock(return_value=5),
    )
    monkeypatch.setattr(
        "app.tasks.purge_raw_data._purge_request_content",
        AsyncMock(return_value=6),
    )
    monkeypatch.setattr(
        "app.tasks.purge_raw_data._purge_export_temp_files",
        MagicMock(return_value=8),
    )

    result = await _purge_body(_build_cfg(), MagicMock())

    assert result == PurgeStats(
        telegram_raw=1,
        crawl_content=2,
        llm_payload=3,
        video_transcript=4,
        downloaded_media=7,
        interaction_text=5,
        request_content=6,
        export_temp_files=8,
    )


@pytest.mark.asyncio
async def test_no_retention_mode_uses_immediate_raw_ttl(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import _purge_body

    telegram = AsyncMock(return_value=0)
    crawl = AsyncMock(return_value=0)
    llm = AsyncMock(return_value=0)
    video = AsyncMock(return_value=0)
    media = AsyncMock(return_value=0)
    interaction = AsyncMock(return_value=0)
    request = AsyncMock(return_value=0)
    monkeypatch.setattr("app.tasks.purge_raw_data._purge_telegram_raw", telegram)
    monkeypatch.setattr("app.tasks.purge_raw_data._purge_crawl_content", crawl)
    monkeypatch.setattr("app.tasks.purge_raw_data._purge_llm_payload", llm)
    monkeypatch.setattr("app.tasks.purge_raw_data._purge_video_transcript", video)
    monkeypatch.setattr("app.tasks.purge_raw_data._purge_downloaded_media", media)
    monkeypatch.setattr("app.tasks.purge_raw_data._purge_interaction_text", interaction)
    monkeypatch.setattr("app.tasks.purge_raw_data._purge_request_content", request)
    monkeypatch.setattr(
        "app.tasks.purge_raw_data._purge_export_temp_files", MagicMock(return_value=0)
    )

    await _purge_body(_build_cfg(privacy_no_retention_mode=True), MagicMock())

    for purge in (telegram, crawl, llm, video, media, interaction, request):
        assert purge.await_args.args[2] == -1


@pytest.mark.asyncio
async def test_purge_crawl_content_updates_raw_columns_without_summaries(monkeypatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks.purge_raw_data import _purge_crawl_content

    mock_db = _make_mock_db(rowcount=1)
    now = dt.datetime.now(dt.UTC)

    await _purge_crawl_content(mock_db, now, days=7, batch=100)

    session = await mock_db.transaction.return_value.__aenter__()
    stmt = session.execute.await_args.args[0]
    compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "crawl_results" in compiled
    assert "summaries" not in compiled
