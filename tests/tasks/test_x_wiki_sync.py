"""Tests for app.tasks.x_wiki_sync (Taskiq wiki delta-scan).

ONE focused test, two assertions per the Step 5.4 acceptance criteria:

  * The task body short-circuits when ``cfg.x_bookmarks.enabled`` is False
    (no runtime construction, empty summary).
  * When enabled, the task body delegates to ``XWikiSyncService.sync()``
    via the runtime and returns its ``WikiSyncSummary`` unchanged.

Mirrors the shape of ``tests/tasks/test_x_bookmarks_sync.py``: fake taskiq
modules so importing ``app.tasks.x_wiki_sync`` does not require the
real Taskiq runtime, then a fake runtime + fake service injected via
``monkeypatch``.
"""

from __future__ import annotations

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
    library_path: str = "/x_bookmarks/library",
    wiki_sync_cron: str = "0 * * * *",
) -> SimpleNamespace:
    return SimpleNamespace(
        x_bookmarks=SimpleNamespace(
            enabled=enabled,
            wiki_sync_cron=wiki_sync_cron,
            library_path=library_path,
        ),
    )


@pytest.mark.asyncio
async def test_sync_short_circuits_when_disabled_else_delegates_to_service(monkeypatch):
    """Combined acceptance: short-circuit when disabled, delegate when enabled."""
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _evict_app_tasks()

    from app.application.services.x_wiki_sync import WikiSyncSummary
    from app.tasks.x_wiki_sync import _wiki_sync_body

    # --- short-circuit when disabled ------------------------------------------------
    runtime_spy = MagicMock()
    monkeypatch.setattr(
        "app.tasks.x_wiki_sync.build_x_wiki_sync_task_runtime",
        runtime_spy,
    )

    disabled_summary = await _wiki_sync_body(_build_cfg(enabled=False), MagicMock())

    assert disabled_summary == WikiSyncSummary()
    runtime_spy.assert_not_called()

    # --- delegate when enabled ------------------------------------------------------
    expected = WikiSyncSummary(
        files_seen=4,
        files_changed=2,
        files_skipped=1,
        orphans_deleted=1,
    )
    service = SimpleNamespace(sync=AsyncMock(return_value=expected))
    monkeypatch.setattr(
        "app.tasks.x_wiki_sync.build_x_wiki_sync_task_runtime",
        lambda cfg, db: SimpleNamespace(cfg=cfg, db=db, service=service),
    )

    enabled_summary = await _wiki_sync_body(_build_cfg(enabled=True), MagicMock())

    service.sync.assert_awaited_once()
    assert enabled_summary == expected
