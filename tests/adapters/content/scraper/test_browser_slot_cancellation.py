"""A cancelled browser scrape must not hand its slot back while the browser lives.

The chain races the browser tier and cancels the losers, so cancellation here is
the normal path. `asyncio.to_thread` does not stop a thread that has already
started, so the old `async with semaphore: await asyncio.to_thread(...)` freed
the slot the instant the await was cancelled while Chromium kept running -- the
next URL launched another one, and the live browser count drifted above the cap
that exists to keep a memory-capped container off the OOM killer.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from app.adapters.content.scraper.browser_concurrency import run_holding_slot


async def test_slot_stays_held_until_the_thread_finishes() -> None:
    semaphore = asyncio.Semaphore(1)
    started = threading.Event()
    may_finish = threading.Event()
    finished = threading.Event()

    def _blocking() -> str:
        started.set()
        may_finish.wait(timeout=5)
        finished.set()
        return "done"

    task = asyncio.create_task(run_holding_slot(semaphore, _blocking))
    await asyncio.to_thread(started.wait, 5)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert semaphore.locked(), (
        "the slot came back while the browser was still running, so the cap no "
        "longer bounds the number of live browser processes"
    )

    may_finish.set()
    await asyncio.to_thread(finished.wait, 5)
    async with asyncio.timeout(5):
        await semaphore.acquire()
    semaphore.release()


async def test_slot_is_released_after_a_normal_run() -> None:
    semaphore = asyncio.Semaphore(1)

    assert await run_holding_slot(semaphore, lambda: "ok") == "ok"

    assert not semaphore.locked()


async def test_slot_is_released_when_the_work_raises() -> None:
    semaphore = asyncio.Semaphore(1)

    def _boom() -> str:
        raise RuntimeError("launch failed")

    with pytest.raises(RuntimeError, match="launch failed"):
        await run_holding_slot(semaphore, _boom)

    assert not semaphore.locked()


async def test_the_next_scrape_cannot_overtake_a_cancelled_one() -> None:
    """The chain's actual sequence: a loser is cancelled, the next URL arrives.

    This is the property the cap exists for. Under the old spelling the second
    launch started immediately against a cap of one, because the first slot came
    back on cancellation rather than on the browser exiting.
    """
    semaphore = asyncio.Semaphore(1)
    live = 0
    peak = 0
    guard = threading.Lock()
    release_all = threading.Event()
    first_started = threading.Event()

    def _blocking() -> str:
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        first_started.set()
        release_all.wait(timeout=5)
        with guard:
            live -= 1
        return "done"

    loser = asyncio.create_task(run_holding_slot(semaphore, _blocking))
    await asyncio.to_thread(first_started.wait, 5)

    loser.cancel()
    with pytest.raises(asyncio.CancelledError):
        await loser

    # The next URL's browser attempt, arriving while the cancelled one is alive.
    successor = asyncio.create_task(run_holding_slot(semaphore, _blocking))
    await asyncio.sleep(0.2)

    assert peak == 1, f"{peak} browsers ran concurrently against a cap of 1"

    release_all.set()
    async with asyncio.timeout(5):
        await successor
