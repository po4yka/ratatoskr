"""Taskiq scheduler: emits channel_digest and rss_poll tasks at configured times.

Run as a separate process:
    taskiq scheduler app.tasks.scheduler:scheduler [--skip-first-run]

The scheduler does NOT execute tasks; it only enqueues them onto the broker.
Tasks are consumed and executed by the worker process.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

try:
    from app.observability.otel import init_tracing as _init_tracing

    _init_tracing()
except Exception:  # pragma: no cover
    pass

from taskiq import TaskiqScheduler
from taskiq.abc.schedule_source import ScheduleSource
from taskiq.scheduler.scheduled_task import ScheduledTask

from app.config import load_config
from app.observability.metrics_http import start_metrics_http_server_from_env
from app.observability.otel import get_tracer
from app.tasks.broker import broker

start_metrics_http_server_from_env()


def _install_sigterm_cancel() -> None:
    """Turn SIGTERM into a cancel of the scheduler's main task.

    taskiq's *worker* CLI installs signal handlers; its *scheduler* CLI installs
    none, and its teardown -- ``scheduler.shutdown()`` then ``source.shutdown()``
    -- sits inside ``except asyncio.CancelledError``. Docker stops a container
    with SIGTERM, Python leaves it at SIG_DFL, so this process died on the spot:
    no taskiq teardown, and no ``atexit``, which is what
    ``TracerProvider(shutdown_on_exit=True)`` relies on to flush the span buffer.
    Every stop dropped whatever was buffered.

    Cancelling the main task is enough to recover both. ``run_scheduler`` is what
    ``asyncio.run`` is running (taskiq/cli/scheduler/cmd.py), so the cancel lands
    in the branch taskiq already wrote for it, the coroutine then returns
    normally, and the interpreter exits through ``atexit``. No explicit flush is
    needed here, and no blocking export runs inside a signal handler.

    Installed from ``startup()`` rather than at import: this module is imported
    by the test suite too, and a process-wide handler has no business appearing
    there. SIGINT is left alone -- ``asyncio.Runner`` already cancels on it.
    """
    task = asyncio.current_task()
    if task is None:  # pragma: no cover - startup() always runs inside the task
        return
    # NotImplementedError on platforms without add_signal_handler (Windows).
    with contextlib.suppress(NotImplementedError, RuntimeError):
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)


class _TracedScheduler(TaskiqScheduler):
    """Open a span around the enqueue so the worker's task span has a parent.

    ``OTelPropagationMiddleware.pre_send`` injects the *current* context into the
    message labels, and taskiq runs middleware before ``broker.kick``. This
    process had no active span at that moment, so ``inject`` wrote nothing
    (measured: an empty dict) and every cron-triggered ``taskiq.<task>`` span in
    the worker was a root with no link back to what scheduled it. The middleware
    is not dead code -- it works for kicks made from the bot or the API inside a
    live request span -- it just had no context to carry on this hop.
    """

    async def on_ready(self, source: ScheduleSource, task: ScheduledTask) -> None:
        with get_tracer("ratatoskr.scheduler").start_as_current_span(
            f"scheduler.enqueue {task.task_name}",
            attributes={"taskiq.task_name": task.task_name},
        ):
            await super().on_ready(source, task)


def _minutes_to_cron(n: int) -> str:
    """Convert a poll interval in minutes to a cron expression.

    For n<=60 returns '*/n * * * *'.  For n>60 aligns to whole hours
    (e.g. 120 -> '0 */2 * * *').  Values that do not divide evenly into 60
    are rounded down (e.g. 90 -> '0 */1 * * *').

    Note: APScheduler's IntervalTrigger fires exactly n minutes after the
    previous run; cron aligns to wall-clock multiples.  Acceptable difference
    for RSS polling cadence.
    """
    if n <= 60:
        return f"*/{n} * * * *"
    return f"0 */{n // 60} * * *"


class _AppConfigScheduleSource(ScheduleSource):
    """Generates ScheduledTask entries from AppConfig at scheduler startup.

    Reads DIGEST_TIMES (cron per delivery time) and RSS_POLL_INTERVAL_MINUTES
    (interval converted to cron).  No runtime mutation — redeploy to change.

    load_config() is deferred to the first get_schedules() call so that
    importing this module (e.g. during test collection) doesn't require env
    vars to be set.
    """

    def __init__(self) -> None:
        self._tasks: list[ScheduledTask] | None = None

    async def startup(self) -> None:
        """taskiq awaits this inside the task it runs; see _install_sigterm_cancel."""
        _install_sigterm_cancel()

    def _build_tasks(self) -> list[ScheduledTask]:
        cfg = load_config()
        tasks: list[ScheduledTask] = []

        if cfg.digest.enabled:
            for time_str in cfg.digest.digest_times:
                h, m = map(int, time_str.split(":"))
                tasks.append(
                    ScheduledTask(
                        task_name="ratatoskr.digest.run",
                        cron=f"{m} {h} * * *",
                        cron_offset=cfg.digest.timezone,
                        labels={"job": f"digest_{time_str}"},
                        args=[],
                        kwargs={},
                    )
                )

        signal_sources_enabled = bool(getattr(cfg.signal_ingestion, "any_enabled", False))
        if cfg.rss.enabled or signal_sources_enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.rss.poll",
                    cron=_minutes_to_cron(cfg.rss.poll_interval_minutes),
                    labels={"job": "rss_poll"},
                    args=[],
                    kwargs={},
                )
            )
        if signal_sources_enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.topic_change_watch.run",
                    cron="15 * * * *",
                    labels={"job": "topic_change_watch"},
                    args=[],
                    kwargs={},
                )
            )

        if cfg.github.sync_enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.github.sync_stars",
                    cron=cfg.github.sync_cron,
                    labels={"job": "github_stars_sync"},
                    args=[],
                    kwargs={},
                )
            )

        if cfg.vector_reconcile.enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.vector.reconcile",
                    cron=cfg.vector_reconcile.cron,
                    labels={"job": "vector_reconcile"},
                    args=[],
                    kwargs={},
                )
            )

        if cfg.retention.enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.data.purge",
                    cron=cfg.retention.cron,
                    labels={"job": "data_purge"},
                    args=[],
                    kwargs={},
                )
            )

        if cfg.retention.source_content_reconcile_enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.source_content.reconcile",
                    cron=cfg.retention.source_content_reconcile_cron,
                    labels={"job": "source_content_reconcile"},
                    args=[],
                    kwargs={},
                )
            )

        if cfg.x_bookmarks.enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.x.sync_bookmarks",
                    cron=cfg.x_bookmarks.sync_cron,
                    labels={"job": "x_bookmarks_sync"},
                    args=[],
                    kwargs={},
                )
            )
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.x.sync_wiki",
                    cron=cfg.x_bookmarks.wiki_sync_cron,
                    labels={"job": "x_wiki_sync"},
                    args=[],
                    kwargs={},
                )
            )

        if cfg.git_backup.enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.git_backup.sync",
                    cron=cfg.git_backup.sync_cron,
                    labels={"job": "git_backup_sync"},
                    args=[],
                    kwargs={},
                )
            )

        if cfg.ai_backup.enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.ai_backup.sync",
                    cron=cfg.ai_backup.sync_cron,
                    labels={"job": "ai_backup_sync"},
                    args=[],
                    kwargs={},
                )
            )

        if cfg.background.job_reaper_enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.jobs.reap",
                    cron=cfg.background.job_reaper_cron,
                    labels={"job": "job_reaper"},
                    args=[],
                    kwargs={},
                )
            )

        if cfg.langgraph_checkpoint.enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.langgraph.prune",
                    cron=cfg.langgraph_checkpoint.prune_cron,
                    labels={"job": "langgraph_prune"},
                    args=[],
                    kwargs={},
                )
            )

        return tasks

    async def get_schedules(self) -> list[ScheduledTask]:
        if self._tasks is None:
            self._tasks = self._build_tasks()
        return self._tasks


scheduler = _TracedScheduler(broker=broker, sources=[_AppConfigScheduleSource()])
