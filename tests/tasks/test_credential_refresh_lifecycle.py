"""Tests for the worker's credential hot-reload lifecycle hooks.

``app.tasks.url_processing`` registers WORKER_STARTUP / WORKER_SHUTDOWN
handlers that mirror bot.py's mechanism: ``start_credential_refresh_task``
polls the CredentialStore and swaps changes into this process's ConfigHolder
(``get_app_config``) so a credential saved via the web UI reaches the
worker's long-lived LLM client without a restart.

These tests use the real taskiq ``InMemoryBroker`` (``TASKIQ_BROKER=memory``),
not the lightweight stub used in ``test_url_processing_task.py``: under that
stub, ``broker.on_event(...)`` is itself a MagicMock, so the decorator
returns a mock instead of the original coroutine and the hooks below could
never actually be exercised.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _evict_app_tasks() -> None:
    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)


def test_credential_refresh_hooks_registered_on_broker(monkeypatch):
    """The hooks must actually be wired to WORKER_STARTUP / WORKER_SHUTDOWN."""
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from taskiq import TaskiqEvents

    from app.tasks import url_processing
    from app.tasks.broker import broker

    assert (
        url_processing._start_credential_refresh
        in broker.event_handlers[TaskiqEvents.WORKER_STARTUP]
    )
    assert (
        url_processing._stop_credential_refresh
        in broker.event_handlers[TaskiqEvents.WORKER_SHUTDOWN]
    )


@pytest.mark.asyncio
async def test_start_credential_refresh_starts_task_for_configured_owner(monkeypatch):
    """WORKER_STARTUP must launch the refresh loop against this process's holder."""
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks import url_processing

    cfg = SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=[42]))
    db = object()
    monkeypatch.setattr(url_processing, "get_app_config", AsyncMock(return_value=cfg))
    monkeypatch.setattr(url_processing, "get_db", AsyncMock(return_value=db))

    captured: dict[str, object] = {}
    background_task = asyncio.create_task(asyncio.sleep(1000))
    fake_store = object()

    def _fake_start(holder, store, *, owner_id):
        captured["holder"] = holder
        captured["store"] = store
        captured["owner_id"] = owner_id
        return background_task

    url_processing._credential_refresh_task = None
    try:
        with (
            patch(
                "app.config.credential_reloader.start_credential_refresh_task",
                new=_fake_start,
            ),
            patch(
                "app.infrastructure.persistence.credential_store.CredentialStore",
                new=MagicMock(return_value=fake_store),
            ),
        ):
            await url_processing._start_credential_refresh(MagicMock())

        assert captured["holder"] is cfg
        assert captured["store"] is fake_store
        assert captured["owner_id"] == 42
        assert url_processing._credential_refresh_task is background_task
    finally:
        background_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await background_task
        url_processing._credential_refresh_task = None


@pytest.mark.asyncio
async def test_start_credential_refresh_skips_without_owner(monkeypatch):
    """No allowed_user_ids configured -- must not touch the DB or start a task."""
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks import url_processing

    cfg = SimpleNamespace(telegram=SimpleNamespace(allowed_user_ids=[]))
    monkeypatch.setattr(url_processing, "get_app_config", AsyncMock(return_value=cfg))
    monkeypatch.setattr(
        url_processing,
        "get_db",
        AsyncMock(side_effect=AssertionError("get_db should not be called without an owner")),
    )

    url_processing._credential_refresh_task = None
    await url_processing._start_credential_refresh(MagicMock())

    assert url_processing._credential_refresh_task is None


@pytest.mark.asyncio
async def test_stop_credential_refresh_cancels_cleanly(monkeypatch):
    """WORKER_SHUTDOWN must cancel the loop and await it without raising."""
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks import url_processing

    async def _forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_forever())
    url_processing._credential_refresh_task = task

    await url_processing._stop_credential_refresh(MagicMock())

    assert task.cancelled()
    assert url_processing._credential_refresh_task is None


@pytest.mark.asyncio
async def test_stop_credential_refresh_noop_when_never_started(monkeypatch):
    """Shutdown must tolerate a process that never started the refresh loop."""
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.tasks import url_processing

    url_processing._credential_refresh_task = None

    await url_processing._stop_credential_refresh(MagicMock())

    assert url_processing._credential_refresh_task is None
