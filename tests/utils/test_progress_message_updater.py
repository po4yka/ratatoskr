import asyncio

import pytest

from app.utils.progress_message_updater import ProgressMessageUpdater


class _Tracker:
    def __init__(self) -> None:
        self.updates: list[tuple[object, str, str | None]] = []
        self.finalized: list[tuple[object, str, str | None]] = []

    async def update(self, message: object, text: str, *, parse_mode: str | None = None) -> None:
        self.updates.append((message, text, parse_mode))

    async def finalize(self, message: object, text: str, *, parse_mode: str | None = None) -> None:
        self.finalized.append((message, text, parse_mode))


@pytest.mark.asyncio
async def test_progress_message_updater_updates_formatter_and_finalizes() -> None:
    tracker = _Tracker()
    message = object()
    updater = ProgressMessageUpdater(tracker, message, update_interval=0.01, parse_mode="Markdown")

    await updater.start(lambda elapsed: f"first {elapsed >= 0}")
    await asyncio.sleep(0.02)
    await updater.update_formatter(lambda elapsed: f"second {elapsed >= 0}")
    await updater.finalize("done")

    assert tracker.updates
    assert tracker.updates[-1][1].startswith("second")
    assert tracker.updates[-1][2] == "Markdown"
    assert tracker.finalized == [(message, "done", "Markdown")]
    assert updater._task is None


@pytest.mark.asyncio
async def test_progress_message_updater_context_exit_cancels_running_task() -> None:
    tracker = _Tracker()
    updater = ProgressMessageUpdater(tracker, object(), update_interval=10)

    async with updater:
        await updater.start(lambda elapsed: "working")
        assert updater._task is not None

    assert updater._task is None
