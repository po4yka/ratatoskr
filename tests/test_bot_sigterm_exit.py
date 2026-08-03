"""A shutdown we asked for must exit 0.

20cc545a turned SIGTERM into a cancel of the main task so bot.py's teardown
finally would actually run -- Docker stops a container with SIGTERM, and Python
leaves it at SIG_DFL, so nothing in that block had ever run on a deploy.

The steady-state stop was already clean: idle() catches CancelledError and
returns (app/adapters/telegram/telegram_client.py:206-211), so the cancel unwinds
through both finallys and main() returns normally. What was not clean is a signal
arriving outside that window -- during startup, while db.migrate() or
broker.startup() is awaiting -- where nothing catches it and the CancelledError
escapes asyncio.run. That is a traceback and exit 1 for a shutdown the operator
requested, on exactly the restart a deploy performs.

The two async tests below are the demonstration, not decoration: they run the
real structure and show which window produces which outcome.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import pytest

BOT = Path(__file__).resolve().parents[1] / "bot.py"


async def _run_with_sigterm(*, catches_cancel: bool) -> tuple[str, list[str]]:
    """Mirror bot.py: a cancel-on-SIGTERM main task with a teardown finally.

    ``catches_cancel`` models the idle() window, which swallows the cancel and
    returns, versus a startup await that does not.
    """
    teardown: list[str] = []

    async def body() -> None:
        if catches_cancel:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.Event().wait()
            return
        await asyncio.sleep(5)

    async def main() -> None:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()  # stands in for the signal handler firing
        try:
            await body()
        finally:
            teardown.append("sync")
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0)
            teardown.append("async")

    try:
        await main()
    except asyncio.CancelledError:
        return "cancelled", teardown
    return "returned", teardown


@pytest.mark.asyncio
async def test_a_steady_state_stop_unwinds_normally() -> None:
    """idle() swallows the cancel, so the common case never needed a guard."""
    outcome, teardown = await _run_with_sigterm(catches_cancel=True)

    assert outcome == "returned"
    assert teardown == ["sync", "async"]


@pytest.mark.asyncio
async def test_a_stop_during_startup_escapes_and_needs_the_guard() -> None:
    """The case the guard exists for: nothing upstream catches the cancel."""
    outcome, teardown = await _run_with_sigterm(catches_cancel=False)

    assert outcome == "cancelled", "if this ever returns, the guard is dead weight"
    # The teardown still ran; only the exit code was wrong.
    assert teardown == ["sync", "async"]


class TestTheEntrypointGuard:
    @staticmethod
    def _source() -> str:
        return BOT.read_text(encoding="utf-8")

    def test_cancellation_is_treated_as_a_graceful_stop(self) -> None:
        source = self._source()
        assert "except (KeyboardInterrupt, asyncio.CancelledError)" in source, (
            "a SIGTERM outside the idle window exits 1 with a traceback"
        )

    def test_sigterm_is_still_turned_into_a_cancel(self) -> None:
        """The guard is only correct while something asks for the cancellation."""
        source = self._source()
        assert "add_signal_handler" in source
        assert "signal.SIGTERM" in source

    def test_sigint_is_still_left_to_asyncio(self) -> None:
        """asyncio.Runner cancels on SIGINT; overriding loses double-Ctrl-C."""
        assert "signal.SIGINT" not in self._source()
