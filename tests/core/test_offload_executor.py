"""The shared thread pool must not be the constraint nobody chose.

Every ``asyncio.to_thread`` in the process shares one executor that CPython sizes
at ``min(32, cpu_count + 4)`` -- 8 on the 4-core Pi. The Taskiq worker loads every
task module, so git backup, RSS polling and URL processing offload into those same
8 slots, and their own budgets (GIT_BACKUP_WORKERS, RSS_POLL_CONCURRENCY,
TASKIQ_MAX_ASYNC_TASKS_PER_PROCESS) sum higher than that with nothing connecting
them to it. The queue behind the pool is unbounded, so the overflow is silent.

The trap that makes this worth fixing is diagnostic: ``loop.getaddrinfo`` is
pinned to this executor and gates every scrape, so a DNS resolve queued behind a
``git gc`` presents exactly like the Docker embedded-DNS failure already on
record -- providers timing out against a healthy site, resolver fine, loop-lag
monitor silent because the loop is idle rather than lagging.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import threading
from pathlib import Path

import pytest

from app.config.runtime import RuntimeConfig
from app.core.offload import install_default_executor

REPO = Path(__file__).resolve().parents[2]


class TestTheKnob:
    def test_the_default_clears_the_shipped_budgets(self) -> None:
        """It has to exceed what one worker process can ask for at once."""
        from app.config.git_backup import GitBackupConfig
        from app.config.rss import RSSConfig

        runtime = RuntimeConfig()
        worst_case = (
            GitBackupConfig().workers
            + RSSConfig().poll_concurrency
            + runtime.url_worker_concurrency
        )
        assert runtime.offload_max_threads >= worst_case

    def test_it_cannot_be_set_below_the_cpython_default(self) -> None:
        """Below 8 this would be a downgrade on the very host it targets."""
        with pytest.raises(ValueError):
            RuntimeConfig(OFFLOAD_MAX_THREADS=4)

    def test_an_operator_can_raise_it(self) -> None:
        assert RuntimeConfig(OFFLOAD_MAX_THREADS=64).offload_max_threads == 64


class TestInstallation:
    @pytest.mark.asyncio
    async def test_it_replaces_the_loops_default_executor(self) -> None:
        loop = asyncio.get_running_loop()
        install_default_executor(24)

        executor = loop._default_executor
        assert isinstance(executor, concurrent.futures.ThreadPoolExecutor)
        assert executor._max_workers == 24

    @pytest.mark.asyncio
    async def test_to_thread_actually_runs_on_it(self) -> None:
        """A pool nobody dispatches to would be decoration."""
        install_default_executor(12)

        name = await asyncio.to_thread(lambda: threading.current_thread().name)

        assert name.startswith("ratatoskr-offload")

    @pytest.mark.asyncio
    async def test_more_threads_than_cpython_would_have_given_are_usable(self) -> None:
        """The point of the fix: concurrency above whatever CPython would allow.

        The participant count is derived from this machine's own default so the
        test discriminates everywhere. Hard-coding 16 would pass without the fix
        on any host with more than 12 cores, and only fail on the 4-core Pi this
        is actually for.
        """
        cpython_default = min(32, (os.cpu_count() or 1) + 4)
        participants = cpython_default + 8
        install_default_executor(participants)
        barrier = threading.Barrier(participants, timeout=10)

        await asyncio.gather(*(asyncio.to_thread(barrier.wait) for _ in range(participants)))

        assert barrier.n_waiting == 0

    def test_it_does_not_raise_without_a_running_loop(self) -> None:
        """A thread-pool tweak must never be what takes a process down."""
        install_default_executor(16)


class TestEveryOffloadingProcessSizesIt:
    """The scheduler is excluded on purpose: it enqueues and never offloads."""

    @pytest.mark.parametrize(
        "path",
        ["bot.py", "app/api/main.py", "app/tasks/broker.py"],
        ids=["bot", "mobile-api", "taskiq-worker"],
    )
    def test_the_entrypoint_installs_the_executor(self, path: str) -> None:
        source = (REPO / path).read_text()
        assert "install_default_executor" in source, f"{path} keeps CPython's 8-thread default"

    def test_the_worker_sizes_it_before_running_tasks(self) -> None:
        """WORKER_STARTUP, not the first task -- the pool must exist beforehand."""
        source = (REPO / "app/tasks/broker.py").read_text()
        startup = "on_event(_TaskiqEvents.WORKER_STARTUP)"
        shutdown = "on_event(_TaskiqEvents.WORKER_SHUTDOWN)"
        assert startup in source, "the worker never sizes its offload pool"
        # Compare the decorators, not prose that mentions either name.
        assert source.index(startup) < source.index(shutdown)
