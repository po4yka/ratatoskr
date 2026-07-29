"""Coverage for the event-loop lag probe.

The probe is what turns "the loop was probably stalling" into a logged number,
so it has to be right in both directions: silent on a healthy loop, loud when
something blocks it. A probe that cries wolf would be worse than none.

These assert against the module's ``logger`` object rather than ``caplog`` or a
handler on the named logger. ``setup_json_logging`` routes the app through
loguru process-wide, so anything relying on stdlib emission passes in isolation
and fails once another module has configured logging first.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from app.observability import loop_lag


class _SpyLogger:
    """Stands in for the module logger, keeping the fully formatted message."""

    def __init__(self) -> None:
        self.warnings: list[str] = []

    def warning(self, msg: str, *args: Any, **_kwargs: Any) -> None:
        self.warnings.append(msg % args if args else msg)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> _SpyLogger:
    logger = _SpyLogger()
    monkeypatch.setattr(loop_lag, "logger", logger)
    return logger


@pytest.mark.asyncio
async def test_stays_quiet_on_a_healthy_loop(spy: _SpyLogger) -> None:
    monitor = loop_lag.start_loop_lag_monitor(interval_sec=0.02, threshold_ms=200.0)
    try:
        await asyncio.sleep(0.3)
    finally:
        monitor.cancel()
    assert spy.warnings == []


@pytest.mark.asyncio
async def test_reports_a_blocked_loop_with_its_magnitude(spy: _SpyLogger) -> None:
    monitor = loop_lag.start_loop_lag_monitor(interval_sec=0.02, threshold_ms=100.0)
    try:
        await asyncio.sleep(0.05)
        # Suppression is deliberate: blocking the loop IS the condition under
        # test -- an awaited sleep yields, and the probe would then correctly
        # stay silent, testing nothing.
        time.sleep(0.3)  # noqa: ASYNC251 -- simulates torch inference holding the GIL
        await asyncio.sleep(0.05)
    finally:
        monitor.cancel()

    assert spy.warnings, "probe stayed silent through a 300ms block"
    stall = next(m for m in spy.warnings if "event_loop_stalled" in m)
    # The magnitude must survive into the message itself: the worker's bracket
    # formatter drops `extra`, which is exactly how it went missing the first time.
    assert "lag_ms=" in stall
    reported = float(stall.split("lag_ms=")[1].split()[0])
    assert reported >= 100.0


@pytest.mark.asyncio
async def test_cancel_stops_the_probe(spy: _SpyLogger) -> None:
    monitor = loop_lag.start_loop_lag_monitor(interval_sec=0.02, threshold_ms=100.0)
    monitor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await monitor
    assert monitor.cancelled()
