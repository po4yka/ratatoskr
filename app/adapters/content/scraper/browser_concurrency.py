"""Concurrency accounting for provider rungs that launch a real browser.

A browser launch runs in a worker thread because the Playwright/Scrapling APIs
used here are synchronous. Threads are not cancellable: once a pool worker has
picked the job up, ``concurrent.futures.Future.cancel()`` is a no-op and the
thread runs to completion regardless of what the awaiting coroutine does.

That makes the obvious spelling wrong::

    async with semaphore:                 # slot taken
        return await asyncio.to_thread(launch_browser)

Cancelling that await unwinds the ``async with`` immediately, so the slot comes
back while the Chromium it was capping is still alive. The chain cancels losers
on every browser-tier race, so this is the common path, not an edge case: each
race hands back a slot early, the next URL launches another browser, and the
number of live browser processes drifts above the cap the semaphore exists to
enforce -- on a memory-capped container, into the OOM killer.

:func:`run_holding_slot` releases from inside the worker thread instead, so the
slot returns when the browser is actually gone.
"""

from __future__ import annotations

import asyncio
import os
import weakref
from typing import TYPE_CHECKING, Any, TypeVar

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

T = TypeVar("T")

# Keyed per event loop so each semaphore binds lazily to the loop that uses it
# (and stays correct across test loops), then per cap name.
_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]] = (
    weakref.WeakKeyDictionary()
)


def launch_semaphore(name: str, *, env_var: str, default: int) -> asyncio.Semaphore:
    """Return the process-wide launch cap named *name* for the running loop."""
    loop = asyncio.get_running_loop()
    per_loop = _semaphores.setdefault(loop, {})
    semaphore = per_loop.get(name)
    if semaphore is None:
        try:
            limit = max(1, int(os.getenv(env_var, str(default))))
        except ValueError:
            limit = default
        semaphore = asyncio.Semaphore(limit)
        per_loop[name] = semaphore
    return semaphore


def chromium_launch_semaphore() -> asyncio.Semaphore:
    """The single cap on Chromium processes started inside this container.

    Shared by the Playwright and Crawlee rungs rather than one cap each, because
    the chain races the browser tier: both launch their own Chromium for the
    *same* URL, concurrently, by default. Separate caps of two would therefore
    permit four browsers for one request -- worse than the status quo in exactly
    the case that matters, since the resource being protected is one container's
    cgroup memory, and two concurrent in-container Chromium processes are what
    OOM-killed it before.

    The other browser-tier rungs are not counted: CloakBrowser drives a browser in
    its own sidecar container, and ScrapeGraphAI is a hosted API. Scrapling keeps
    its own cap -- it sits in an earlier tier and is not raced against these two.

    Reads ``PLAYWRIGHT_MAX_CONCURRENT_BROWSERS`` so an operator's existing tuning
    keeps working; the name is now narrower than what it governs.
    """
    return launch_semaphore("chromium", env_var="PLAYWRIGHT_MAX_CONCURRENT_BROWSERS", default=2)


async def run_holding_slot(
    semaphore: asyncio.Semaphore,
    func: Callable[..., T],
    /,
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run blocking *func* in a worker thread, holding *semaphore* until it ends.

    Cancelling the returned awaitable stops the caller waiting, not the thread.
    The slot stays taken until *func* returns, which is the honest accounting:
    the resource the semaphore caps is still in use. Providers that can stop
    their own work early should also pass a cancellation flag *func* checks --
    holding the slot keeps the cap correct, but only the provider can make the
    browser exit sooner.
    """
    loop = asyncio.get_running_loop()
    await semaphore.acquire()

    def _release() -> None:
        try:
            loop.call_soon_threadsafe(semaphore.release)
        except RuntimeError:
            # Loop already closed (interpreter/container shutdown). The process
            # is going away with the browser, so there is no cap left to keep.
            logger.debug("browser_slot_release_skipped_loop_closed")

    def _run() -> T:
        try:
            return func(*args, **kwargs)
        finally:
            _release()

    return await asyncio.to_thread(_run)
