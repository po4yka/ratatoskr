"""Taskiq scheduler: emits channel_digest and rss_poll tasks at configured times.

Run as a separate process:
    taskiq scheduler app.tasks.scheduler:scheduler [--skip-first-run]

The scheduler does NOT execute tasks; it only enqueues them onto the broker.
Tasks are consumed and executed by the worker process.
"""

from __future__ import annotations

try:
    from app.observability.otel import init_tracing as _init_tracing

    _init_tracing()
except Exception:  # pragma: no cover
    pass

from taskiq import TaskiqScheduler
from taskiq.abc.schedule_source import ScheduleSource
from taskiq.scheduler.scheduled_task import ScheduledTask

from app.config import load_config
from app.tasks.broker import broker


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

        if cfg.fieldtheory.enabled:
            tasks.append(
                ScheduledTask(
                    task_name="ratatoskr.fieldtheory.sync_bookmarks",
                    cron=cfg.fieldtheory.sync_cron,
                    labels={"job": "fieldtheory_sync"},
                    args=[],
                    kwargs={},
                )
            )

        return tasks

    async def get_schedules(self) -> list[ScheduledTask]:
        if self._tasks is None:
            self._tasks = self._build_tasks()
        return self._tasks


scheduler = TaskiqScheduler(broker=broker, sources=[_AppConfigScheduleSource()])
