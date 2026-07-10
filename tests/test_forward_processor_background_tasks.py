"""ForwardProcessor must hold strong references to its fire-and-forget tasks
and drain them on shutdown.

Without a registry the event loop keeps only a weak reference and can GC an
insights / related-reads task mid-run; without aclose() nothing waits for them
on shutdown. These tests exercise the scheduling + drain methods in isolation
(the full constructor pulls in many collaborators none of these methods touch).
"""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from app.adapters.telegram.forward_processor import ForwardProcessor

pytestmark = pytest.mark.no_network


def _make_proc() -> ForwardProcessor:
    proc = ForwardProcessor.__new__(ForwardProcessor)
    proc._background_tasks = set()
    return proc


@pytest.mark.asyncio
async def test_scheduled_task_is_registered_then_discarded() -> None:
    proc = _make_proc()

    async def _quick() -> str:
        return "ok"

    task = proc._schedule_background_task(_quick(), "cid", "insights")

    assert task is not None
    # Strong reference held while the task is in flight.
    assert task in proc._background_tasks

    result = await task
    await asyncio.sleep(0)  # let the discard done-callback run

    assert result == "ok"
    assert proc._background_tasks == set()


@pytest.mark.asyncio
async def test_aclose_drains_pending_tasks() -> None:
    proc = _make_proc()

    async def _work() -> str:
        await asyncio.sleep(0.02)
        return "done"

    task = proc._schedule_background_task(_work(), "cid", "related_reads")
    assert task is not None and task in proc._background_tasks

    await proc.aclose(timeout=1.0)
    await asyncio.sleep(0)

    assert task.done()
    assert proc._background_tasks == set()


@pytest.mark.asyncio
async def test_aclose_no_op_when_no_tasks() -> None:
    proc = _make_proc()
    # Must not raise or hang with an empty registry.
    await proc.aclose(timeout=0.5)


@pytest.mark.asyncio
async def test_aclose_gives_up_on_timeout_and_cancels_stragglers() -> None:
    proc = _make_proc()

    async def _slow() -> None:
        await asyncio.sleep(1.0)

    task = proc._schedule_background_task(_slow(), "cid", "slow")
    assert task is not None

    # aclose waits up to the timeout, then gives up (logs a timeout) without
    # raising; the still-running task is cancelled rather than left dangling.
    with contextlib.suppress(asyncio.CancelledError):
        await proc.aclose(timeout=0.02)
    await asyncio.sleep(0)

    assert task.done()
    assert task.cancelled()
