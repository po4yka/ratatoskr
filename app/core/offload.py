"""Size the process-wide thread executor that every ``asyncio.to_thread`` shares.

``asyncio.to_thread`` submits to the event loop's default ThreadPoolExecutor,
which CPython creates lazily at ``min(32, cpu_count + 4)`` -- 8 threads on the
4-core Pi. Roughly 150 call sites across the codebase share those 8 slots, and
the executor's work queue is unbounded, so oversubscription does not fail: it
queues silently and every later offload waits behind whatever is already there.

The subsystems that do the heavy offloading already carry their own operator-
tunable bounds -- ``RSS_POLL_CONCURRENCY`` (8), ``GIT_BACKUP_WORKERS`` (4, and
its validator permits 32), ``TASKIQ_MAX_ASYNC_TASKS_PER_PROCESS`` -- and the
Taskiq worker loads all of those task modules into one process. Their sum
exceeds 8, and nothing connects any of those knobs to the pool behind them. So
the default pool, which no operator ever chose, quietly becomes the real limit,
and one subsystem's offloads stall another's.

The consequence worth naming is a diagnosis trap rather than an outage.
``loop.getaddrinfo`` runs through this same executor -- CPython pins it there and
it cannot be moved -- and it gates every outbound scrape. A DNS resolve queued
behind a ``git gc`` looks, from the outside, exactly like the Docker embedded-DNS
failure this project has already debugged once: providers timing out against a
site that is up, with Redis idle and the resolver healthy. The event-loop lag
monitor stays silent throughout, because the loop is not lagging -- it is idle,
waiting on futures that were never dispatched.

Raising the ceiling is cheap. A thread parked in ``subprocess.run`` or a socket
read costs no CPU, threads are created on demand so a high limit costs nothing
until it is used, and the real resource bounds stay where they belong: each
subsystem's own semaphore, and the container's CPU quota.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from app.core.logging_utils import get_logger

logger = get_logger(__name__)


def install_default_executor(max_threads: int) -> None:
    """Replace the running loop's default executor with an explicitly sized one.

    Call once per process, early, before anything offloads. Safe to call when no
    loop is running -- it logs and returns rather than raising, so a caller in an
    unusual startup path cannot take the process down over a thread-pool tweak.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("offload_executor_no_running_loop", extra={"max_threads": max_threads})
        return

    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=max_threads, thread_name_prefix="ratatoskr-offload")
    )
    logger.info("offload_executor_installed", extra={"max_threads": max_threads})
