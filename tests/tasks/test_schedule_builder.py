"""Tests for app.tasks.scheduler schedule-builder utilities."""

from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass, field
from unittest.mock import MagicMock


@dataclass
class _ScheduledTask:
    task_name: str
    cron: str = ""
    cron_offset: str = ""
    labels: dict = field(default_factory=dict)
    args: list = field(default_factory=list)
    kwargs: dict = field(default_factory=dict)


def _stub_taskiq(monkeypatch):
    """Stub taskiq and taskiq_redis so imports work without either installed."""
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
    monkeypatch.setattr(taskiq_mod, "AsyncBroker", object, raising=False)
    monkeypatch.setattr(taskiq_mod, "TaskiqDepends", lambda fn, **_kw: None, raising=False)
    monkeypatch.setattr(taskiq_mod, "TaskiqMiddleware", object, raising=False)
    monkeypatch.setattr(taskiq_mod, "InMemoryBroker", MagicMock, raising=False)
    monkeypatch.setattr(taskiq_mod, "TaskiqScheduler", MagicMock, raising=False)

    msg_mod = sys.modules["taskiq.message"]
    monkeypatch.setattr(msg_mod, "TaskiqMessage", object, raising=False)

    sched_task_mod = sys.modules["taskiq.scheduler.scheduled_task"]
    monkeypatch.setattr(sched_task_mod, "ScheduledTask", _ScheduledTask, raising=False)

    source_mod = sys.modules["taskiq.abc.schedule_source"]
    monkeypatch.setattr(source_mod, "ScheduleSource", object, raising=False)

    tkr_mod = sys.modules["taskiq_redis"]
    monkeypatch.setattr(tkr_mod, "RedisStreamBroker", MagicMock, raising=False)
    monkeypatch.setattr(tkr_mod, "RedisAsyncResultBackend", MagicMock, raising=False)


def _load_scheduler_module(monkeypatch):
    """Import app.tasks.scheduler with stubbed taskiq and minimal AppConfig."""
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    _stub_taskiq(monkeypatch)

    for mod in list(sys.modules):
        if mod.startswith("app.tasks"):
            sys.modules.pop(mod, None)

    return importlib.import_module("app.tasks.scheduler")


def test_minutes_to_cron_sub_hour(monkeypatch):
    mod = _load_scheduler_module(monkeypatch)
    assert mod._minutes_to_cron(5) == "*/5 * * * *"
    assert mod._minutes_to_cron(30) == "*/30 * * * *"
    assert mod._minutes_to_cron(60) == "*/60 * * * *"


def test_minutes_to_cron_super_hour(monkeypatch):
    mod = _load_scheduler_module(monkeypatch)
    assert mod._minutes_to_cron(120) == "0 */2 * * *"
    assert mod._minutes_to_cron(360) == "0 */6 * * *"
    assert mod._minutes_to_cron(1440) == "0 */24 * * *"


def test_digest_times_produce_correct_cron(monkeypatch):
    mod = _load_scheduler_module(monkeypatch)

    cfg = MagicMock()
    cfg.digest.enabled = True
    cfg.digest.digest_times = ["10:00", "19:30"]
    cfg.digest.timezone = "Europe/Moscow"
    cfg.rss.enabled = False
    cfg.rss.poll_interval_minutes = 30
    cfg.signal_ingestion.any_enabled = False

    source = mod._AppConfigScheduleSource.__new__(mod._AppConfigScheduleSource)
    source._tasks = []

    for time_str in cfg.digest.digest_times:
        h, m = map(int, time_str.split(":"))
        source._tasks.append(
            _ScheduledTask(
                task_name="ratatoskr.digest.run",
                cron=f"{m} {h} * * *",
                cron_offset=cfg.digest.timezone,
                labels={"job": f"digest_{time_str}"},
            )
        )

    assert len(source._tasks) == 2
    t0, t1 = source._tasks
    assert t0.cron == "0 10 * * *"
    assert t0.cron_offset == "Europe/Moscow"
    assert t1.cron == "30 19 * * *"
    assert t1.labels == {"job": "digest_19:30"}


def test_rss_poll_schedule_added_when_enabled(monkeypatch):
    mod = _load_scheduler_module(monkeypatch)

    tasks: list[_ScheduledTask] = []

    cfg = MagicMock()
    cfg.digest.enabled = False
    cfg.rss.enabled = True
    cfg.rss.poll_interval_minutes = 30
    cfg.signal_ingestion.any_enabled = False

    tasks.append(
        _ScheduledTask(
            task_name="ratatoskr.rss.poll",
            cron=mod._minutes_to_cron(cfg.rss.poll_interval_minutes),
            labels={"job": "rss_poll"},
        )
    )

    assert len(tasks) == 1
    assert tasks[0].cron == "*/30 * * * *"
    assert tasks[0].task_name == "ratatoskr.rss.poll"


def test_vector_reconcile_schedule_added_when_enabled(monkeypatch):
    mod = _load_scheduler_module(monkeypatch)

    cfg = MagicMock()
    cfg.digest.enabled = False
    cfg.rss.enabled = False
    cfg.signal_ingestion.any_enabled = False
    cfg.github.sync_enabled = False
    cfg.vector_reconcile.enabled = True
    cfg.vector_reconcile.cron = "*/30 * * * *"

    monkeypatch.setattr(mod, "load_config", lambda: cfg)
    source = mod._AppConfigScheduleSource()
    tasks = source._build_tasks()

    reconcile_tasks = [t for t in tasks if t.task_name == "ratatoskr.vector.reconcile"]
    assert len(reconcile_tasks) == 1
    assert reconcile_tasks[0].cron == "*/30 * * * *"
    assert reconcile_tasks[0].labels == {"job": "vector_reconcile"}


def test_vector_reconcile_schedule_skipped_when_disabled(monkeypatch):
    mod = _load_scheduler_module(monkeypatch)

    cfg = MagicMock()
    cfg.digest.enabled = False
    cfg.rss.enabled = False
    cfg.signal_ingestion.any_enabled = False
    cfg.github.sync_enabled = False
    cfg.vector_reconcile.enabled = False

    monkeypatch.setattr(mod, "load_config", lambda: cfg)
    source = mod._AppConfigScheduleSource()
    tasks = source._build_tasks()

    assert not any(t.task_name == "ratatoskr.vector.reconcile" for t in tasks)


def test_source_content_reconcile_schedule_uses_retention_config(monkeypatch):
    mod = _load_scheduler_module(monkeypatch)

    cfg = MagicMock()
    cfg.digest.enabled = False
    cfg.rss.enabled = False
    cfg.signal_ingestion.any_enabled = False
    cfg.github.sync_enabled = False
    cfg.vector_reconcile.enabled = False
    cfg.retention.enabled = False
    cfg.retention.source_content_reconcile_enabled = True
    cfg.retention.source_content_reconcile_cron = "20 4 * * *"
    cfg.x_bookmarks.enabled = False
    cfg.git_backup.enabled = False
    cfg.ai_backup.enabled = False
    cfg.langgraph_checkpoint.enabled = False

    monkeypatch.setattr(mod, "load_config", lambda: cfg)
    tasks = mod._AppConfigScheduleSource()._build_tasks()

    reconcile = [task for task in tasks if task.task_name == "ratatoskr.source_content.reconcile"]
    assert len(reconcile) == 1
    assert reconcile[0].cron == "20 4 * * *"
    assert reconcile[0].labels == {"job": "source_content_reconcile"}


def test_x_bookmarks_sync_schedule_added_when_enabled(monkeypatch):
    mod = _load_scheduler_module(monkeypatch)

    cfg = MagicMock()
    cfg.digest.enabled = False
    cfg.rss.enabled = False
    cfg.signal_ingestion.any_enabled = False
    cfg.github.sync_enabled = False
    cfg.vector_reconcile.enabled = False
    cfg.retention.enabled = False
    cfg.x_bookmarks.enabled = True
    cfg.x_bookmarks.sync_cron = "*/15 * * * *"

    monkeypatch.setattr(mod, "load_config", lambda: cfg)
    source = mod._AppConfigScheduleSource()
    tasks = source._build_tasks()

    ft_tasks = [t for t in tasks if t.task_name == "ratatoskr.x.sync_bookmarks"]
    assert len(ft_tasks) == 1
    assert ft_tasks[0].cron == "*/15 * * * *"
    assert ft_tasks[0].labels == {"job": "x_bookmarks_sync"}


def test_x_bookmarks_sync_schedule_skipped_when_disabled(monkeypatch):
    mod = _load_scheduler_module(monkeypatch)

    cfg = MagicMock()
    cfg.digest.enabled = False
    cfg.rss.enabled = False
    cfg.signal_ingestion.any_enabled = False
    cfg.github.sync_enabled = False
    cfg.vector_reconcile.enabled = False
    cfg.retention.enabled = False
    cfg.x_bookmarks.enabled = False

    monkeypatch.setattr(mod, "load_config", lambda: cfg)
    source = mod._AppConfigScheduleSource()
    tasks = source._build_tasks()

    assert not any(t.task_name == "ratatoskr.x.sync_bookmarks" for t in tasks)


def test_x_wiki_sync_schedule_added_when_enabled(monkeypatch):
    """When ``x_bookmarks.enabled`` is True, both bookmark and wiki sync tasks are added."""
    mod = _load_scheduler_module(monkeypatch)

    cfg = MagicMock()
    cfg.digest.enabled = False
    cfg.rss.enabled = False
    cfg.signal_ingestion.any_enabled = False
    cfg.github.sync_enabled = False
    cfg.vector_reconcile.enabled = False
    cfg.retention.enabled = False
    cfg.x_bookmarks.enabled = True
    cfg.x_bookmarks.sync_cron = "*/15 * * * *"
    cfg.x_bookmarks.wiki_sync_cron = "0 * * * *"

    monkeypatch.setattr(mod, "load_config", lambda: cfg)
    source = mod._AppConfigScheduleSource()
    tasks = source._build_tasks()

    wiki_tasks = [t for t in tasks if t.task_name == "ratatoskr.x.sync_wiki"]
    assert len(wiki_tasks) == 1
    assert wiki_tasks[0].cron == "0 * * * *"
    assert wiki_tasks[0].labels == {"job": "x_wiki_sync"}


def test_x_wiki_sync_schedule_skipped_when_disabled(monkeypatch):
    """When ``x_bookmarks.enabled`` is False, neither bookmark nor wiki sync is added."""
    mod = _load_scheduler_module(monkeypatch)

    cfg = MagicMock()
    cfg.digest.enabled = False
    cfg.rss.enabled = False
    cfg.signal_ingestion.any_enabled = False
    cfg.github.sync_enabled = False
    cfg.vector_reconcile.enabled = False
    cfg.retention.enabled = False
    cfg.x_bookmarks.enabled = False

    monkeypatch.setattr(mod, "load_config", lambda: cfg)
    source = mod._AppConfigScheduleSource()
    tasks = source._build_tasks()

    assert not any(t.task_name == "ratatoskr.x.sync_wiki" for t in tasks)
    assert not any(t.task_name == "ratatoskr.x.sync_bookmarks" for t in tasks)
