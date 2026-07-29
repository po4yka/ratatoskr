"""Event-loop lag monitor for the taskiq worker.

The worker runs taskiq's blocking Redis read on the same event loop as the URL
pipeline's torch inference and Playwright executor work. When that loop stalls
longer than the broker's read margin, ``XREADGROUP`` times out even though Redis
answered on time -- which used to kill the receiver and cancel in-flight tasks
(see the socket_timeout comment in ``app/tasks/broker.py``).

Widening the margin makes those stalls survivable; this makes them *visible*, so
"the loop is stalling" stops being an inference from correlated timestamps and
becomes a logged number. Deliberately a plain sleep-drift probe rather than
asyncio debug mode, which instruments every callback and is far too expensive to
leave on in production.
"""

from __future__ import annotations

import asyncio
import time

from app.core.logging_utils import get_logger

logger = get_logger(__name__)

_PROBE_INTERVAL_SEC = 1.0
_DEFAULT_THRESHOLD_MS = 1000.0


async def _watch(*, interval_sec: float, threshold_ms: float) -> None:
    while True:
        started = time.perf_counter()
        await asyncio.sleep(interval_sec)
        # Drift beyond the requested sleep is time the loop could not hand
        # control back -- i.e. how long a ready callback would have waited.
        lag_ms = (time.perf_counter() - started - interval_sec) * 1000
        if lag_ms >= threshold_ms:
            logger.warning(
                "event_loop_stalled",
                extra={"lag_ms": round(lag_ms, 1), "threshold_ms": threshold_ms},
            )


def start_loop_lag_monitor(
    *,
    interval_sec: float = _PROBE_INTERVAL_SEC,
    threshold_ms: float = _DEFAULT_THRESHOLD_MS,
) -> asyncio.Task[None]:
    """Log ``event_loop_stalled`` whenever the loop is late servicing a sleep."""
    return asyncio.create_task(
        _watch(interval_sec=interval_sec, threshold_ms=threshold_ms),
        name="loop-lag-monitor",
    )
