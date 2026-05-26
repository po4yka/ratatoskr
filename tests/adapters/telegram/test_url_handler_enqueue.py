"""Tests for URLHandler._handle_single_url_enqueue — the worker enqueue path.

Covers:
- When url_worker_enqueue_enabled=True and not batch_mode, the enqueue path is taken
- When url_worker_enqueue_enabled=False, the inline path is taken
- When batch_mode=True, the inline path is taken regardless of the flag
- Enqueue path: request row created, job row inserted, placeholder sent, task kicked
- Enqueue path: on request_repo failure, falls back to inline processing
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.adapters.telegram.url_handler import URLHandler

# ── Taskiq stub ───────────────────────────────────────────────────────────────


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

    msg_mod = sys.modules["taskiq.message"]
    msg_mod.TaskiqMessage = object

    sched_task_mod = sys.modules["taskiq.scheduler.scheduled_task"]
    sched_task_mod.ScheduledTask = MagicMock

    source_mod = sys.modules["taskiq.abc.schedule_source"]
    source_mod.ScheduleSource = object

    tkr_mod = sys.modules["taskiq_redis"]
    tkr_mod.RedisStreamBroker = MagicMock
    tkr_mod.RedisAsyncResultBackend = MagicMock


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_cfg(*, enqueue_enabled: bool = True):
    return SimpleNamespace(
        runtime=SimpleNamespace(
            url_worker_enqueue_enabled=enqueue_enabled,
            url_worker_concurrency=4,
        ),
    )


def _make_message(*, chat_id: int = 100, message_id: int = 10):
    peer = SimpleNamespace(channel_id=chat_id, chat_id=chat_id, user_id=None)
    return SimpleNamespace(
        peer_id=peer,
        chat_id=chat_id,
        id=message_id,
        from_user=SimpleNamespace(id=99),
    )


def _make_request_repo(*, request_id: int = 1):
    repo = MagicMock()
    repo.async_create_request = AsyncMock(return_value=request_id)
    repo.async_update_bot_reply_message_id = AsyncMock()
    return repo


def _make_response_formatter(*, reply_message_id: int = 55):
    fmt = MagicMock()
    fmt.safe_reply_with_id = AsyncMock(return_value=reply_message_id)
    return fmt


def _make_url_processor():
    proc = MagicMock()
    proc.handle_url_flow = AsyncMock(return_value=SimpleNamespace(success=True, request_id=1))
    proc.summary_repo = MagicMock()
    proc.audit_func = MagicMock()
    return proc


def _make_job_repo():
    repo = MagicMock()
    repo.record_pending_enqueue = AsyncMock()
    repo.pending_count = AsyncMock(return_value=1)
    return repo


def _make_kicker():
    """Return a chain of mocks that satisfy .kicker().with_task_id(...).kiq(...)."""
    kicker = MagicMock()
    kicker_chain = MagicMock()
    kicker.return_value = kicker_chain
    kicker_chain.with_task_id = MagicMock(return_value=kicker_chain)
    kicker_chain.kiq = AsyncMock()
    return kicker


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_path_taken_when_enabled(monkeypatch):
    """With enqueue_enabled=True and not batch_mode, the enqueue path runs."""
    _stub_taskiq(monkeypatch)
    # Temporarily evict cached task modules so the stubbed broker is picked up.
    # monkeypatch.delitem restores the original value on teardown.
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    request_repo = _make_request_repo(request_id=7)
    response_formatter = _make_response_formatter(reply_message_id=55)
    url_processor = _make_url_processor()
    job_repo = _make_job_repo()
    kicker = _make_kicker()

    handler = URLHandler(
        db=MagicMock(),
        response_formatter=response_formatter,
        url_processor=url_processor,
        request_repo=request_repo,
        cfg=_make_cfg(enqueue_enabled=True),
    )

    mock_task = MagicMock()
    mock_task.kicker = kicker

    # Patch the task at its source module (imported lazily in production code).
    import app.tasks.url_processing as _url_proc_mod

    monkeypatch.setattr(_url_proc_mod, "process_url_request", mock_task)

    with (
        patch(
            "app.api.background.durable_jobs.RequestProcessingJobRepository",
            new=MagicMock(return_value=job_repo),
        ),
        patch("app.observability.metrics.record_url_enqueue"),
        patch("app.observability.metrics.set_url_processing_queue_depth"),
    ):
        result = await handler.handle_single_url(
            message=_make_message(),
            url="https://example.com",
            correlation_id="cid-test",
            batch_mode=False,
        )

    # Inline URLProcessor should NOT have been called.
    url_processor.handle_url_flow.assert_not_awaited()
    # Request row must have been created.
    request_repo.async_create_request.assert_awaited_once()
    # Job row must have been inserted.
    job_repo.record_pending_enqueue.assert_awaited_once()
    # Placeholder reply must have been sent.
    response_formatter.safe_reply_with_id.assert_awaited_once()
    # bot_reply_message_id must have been persisted.
    request_repo.async_update_bot_reply_message_id.assert_awaited_once()
    # Taskiq task must have been kicked.
    kicker_chain = kicker.return_value
    kicker_chain.kiq.assert_awaited_once_with(request_id=7)
    # Result must be a success stub.
    assert result.success is True


@pytest.mark.asyncio
async def test_inline_path_taken_when_disabled(monkeypatch):
    """With enqueue_enabled=False the legacy inline path runs."""
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    url_processor = _make_url_processor()
    response_formatter = _make_response_formatter()

    handler = URLHandler(
        db=MagicMock(),
        response_formatter=response_formatter,
        url_processor=url_processor,
        cfg=_make_cfg(enqueue_enabled=False),
    )

    result = await handler.handle_single_url(
        message=_make_message(),
        url="https://example.com",
        correlation_id="cid-inline",
        batch_mode=False,
    )

    url_processor.handle_url_flow.assert_awaited_once()


@pytest.mark.asyncio
async def test_inline_path_taken_in_batch_mode(monkeypatch):
    """batch_mode=True bypasses enqueue even when the flag is on."""
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    url_processor = _make_url_processor()
    response_formatter = _make_response_formatter()

    handler = URLHandler(
        db=MagicMock(),
        response_formatter=response_formatter,
        url_processor=url_processor,
        cfg=_make_cfg(enqueue_enabled=True),
    )

    result = await handler.handle_single_url(
        message=_make_message(),
        url="https://example.com",
        correlation_id="cid-batch",
        batch_mode=True,
    )

    url_processor.handle_url_flow.assert_awaited_once()


@pytest.mark.asyncio
async def test_enqueue_falls_back_to_inline_on_request_create_failure(monkeypatch):
    """When async_create_request raises, the handler falls back to inline processing."""
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")

    request_repo = MagicMock()
    request_repo.async_create_request = AsyncMock(side_effect=RuntimeError("db down"))
    response_formatter = _make_response_formatter()
    url_processor = _make_url_processor()
    job_repo = _make_job_repo()

    handler = URLHandler(
        db=MagicMock(),
        response_formatter=response_formatter,
        url_processor=url_processor,
        request_repo=request_repo,
        cfg=_make_cfg(enqueue_enabled=True),
    )

    with (
        patch(
            "app.api.background.durable_jobs.RequestProcessingJobRepository",
            new=MagicMock(return_value=job_repo),
        ),
        patch("app.observability.metrics.record_url_enqueue"),
    ):
        result = await handler.handle_single_url(
            message=_make_message(),
            url="https://example.com",
            correlation_id="cid-fallback",
            batch_mode=False,
        )

    # Falls back to inline processing.
    url_processor.handle_url_flow.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_cfg_uses_inline_path(monkeypatch):
    """When cfg is None (legacy construction), inline path is used."""
    url_processor = _make_url_processor()
    response_formatter = _make_response_formatter()

    handler = URLHandler(
        db=MagicMock(),
        response_formatter=response_formatter,
        url_processor=url_processor,
        cfg=None,
    )

    await handler.handle_single_url(
        message=_make_message(),
        url="https://example.com",
        correlation_id="cid-no-cfg",
        batch_mode=False,
    )

    url_processor.handle_url_flow.assert_awaited_once()
