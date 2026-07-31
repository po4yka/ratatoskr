"""Utilities for tracking and cancelling user-scoped asyncio tasks."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class UserTaskManager:
    """Track active asyncio tasks per Telegram user for cooperative cancellation."""

    def __init__(self) -> None:
        self._tasks: dict[int, set[asyncio.Task[object]]] = {}
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def track(self, uid: int, *, enabled: bool = True) -> AsyncIterator[None]:
        """Register the current task for the provided ``uid`` while the context is active."""
        if not enabled:
            yield
            return

        task = asyncio.current_task()
        if task is None:
            yield
            return

        async with self._lock:
            tasks = self._tasks.setdefault(uid, set())
            tasks.add(task)

        try:
            yield
        finally:
            async with self._lock:
                tasks = self._tasks.get(uid)
                if tasks is not None:
                    tasks.discard(task)
                    if not tasks:
                        self._tasks.pop(uid, None)

    async def cancel(self, uid: int, *, exclude_current: bool = False) -> int:
        """Cancel all active tasks for ``uid`` and return the number of tasks cancelled."""
        current_task = asyncio.current_task() if exclude_current else None
        async with self._lock:
            tasks = list(self._tasks.get(uid, set()))

        cancelled = 0
        for task in tasks:
            if task is current_task:
                continue
            if task.done():
                continue
            task.cancel()
            cancelled += 1
        return cancelled
