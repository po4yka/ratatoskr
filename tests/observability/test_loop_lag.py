"""Coverage for the event-loop lag probe.

The probe is what turns "the loop was probably stalling" into a logged number,
so it has to be right in both directions: silent on a healthy loop, loud when
something blocks it. A probe that cries wolf would be worse than none.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app.observability.loop_lag import start_loop_lag_monitor


@pytest.mark.asyncio
async def test_stays_quiet_on_a_healthy_loop(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.observability.loop_lag")
    monitor = start_loop_lag_monitor(interval_sec=0.02, threshold_ms=200.0)
    try:
        await asyncio.sleep(0.3)
    finally:
        monitor.cancel()
    assert "event_loop_stalled" not in caplog.text


@pytest.mark.asyncio
async def test_reports_a_blocked_loop(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="app.observability.loop_lag")
    monitor = start_loop_lag_monitor(interval_sec=0.02, threshold_ms=100.0)
    try:
        await asyncio.sleep(0.05)
        # Suppression is deliberate: blocking the loop IS the condition under
        # test -- an awaited sleep yields, and the probe would then correctly
        # stay silent, testing nothing.
        time.sleep(0.3)  # noqa: ASYNC251 -- simulates torch inference holding the GIL
        await asyncio.sleep(0.05)
    finally:
        monitor.cancel()
    assert "event_loop_stalled" in caplog.text


@pytest.mark.asyncio
async def test_cancel_stops_the_probe() -> None:
    monitor = start_loop_lag_monitor(interval_sec=0.02, threshold_ms=100.0)
    monitor.cancel()
    with pytest.raises(asyncio.CancelledError):
        await monitor
    assert monitor.cancelled()
