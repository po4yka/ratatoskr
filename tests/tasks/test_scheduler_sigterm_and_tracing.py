"""The scheduler was the one process with no shutdown seam of any kind.

It starts OpenTelemetry at import, and taskiq's *scheduler* CLI installs no
signal handlers -- unlike its *worker* CLI -- while its teardown sits inside
``except asyncio.CancelledError``. Docker stops a container with SIGTERM and
Python leaves it at SIG_DFL, so the process died on the spot: no
``scheduler.shutdown()``, no ``source.shutdown()``, and no ``atexit``, which is
what ``TracerProvider(shutdown_on_exit=True)`` relies on to export the buffer.

Measured under the real ``taskiq scheduler`` CLI with a real ``kill -TERM``:

  before: exit 143, only ``source_startup`` ran, 0 spans exported
  after:  exit 0, ``source_startup source_shutdown atexit``, 1 span exported

The second half of this file is the other end of the same hop. The propagation
middleware injects the *current* context into the message labels and taskiq runs
it before ``broker.kick``; this process had no active span at that moment, so
``inject`` wrote nothing and every cron-triggered ``taskiq.<task>`` span in the
worker was a root with no link back to what scheduled it.

  before: traceparent -> MISSING
  after:  traceparent -> 00-0c60800c...-2b808a87...-01
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import os
import pathlib
import signal
import subprocess
import sys
from types import ModuleType
from typing import Any

import pytest

# The CI unit-test job installs neither the `scheduler` nor the `otel` extra.
taskiq = pytest.importorskip("taskiq", reason="the scheduler extra is not installed")


@pytest.fixture
def scheduler_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """A copy bound to the real taskiq, not to a sibling test's stubs.

    tests/tasks/test_schedule_builder.py reloads app.tasks.scheduler while a
    stubbed taskiq is in sys.modules. monkeypatch puts the stubs back afterwards
    but not the module they were baked into, so whatever is cached by then has a
    fake ScheduleSource for a base class. Dropping the entry first is enough --
    monkeypatch restores it, so the sibling keeps its copy too.
    """
    for name in ("app.tasks.scheduler", "app.tasks.middleware"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return importlib.import_module("app.tasks.scheduler")


class TestSigtermReachesTheTeardown:
    @pytest.mark.asyncio
    async def test_startup_makes_sigterm_cancel_the_running_task(
        self, scheduler_module: ModuleType
    ) -> None:
        """``run_scheduler`` is the task ``asyncio.run`` is running.

        Cancelling it lands in the branch taskiq already wrote for shutdown, so
        one handler recovers both the taskiq teardown and -- by letting the
        interpreter exit normally -- the SDK's own atexit flush.
        """
        # Safety net, installed first: if startup() wires up nothing, this
        # catches the signal instead of SIG_DFL killing the test runner.
        net: list[int] = []
        previous = signal.signal(signal.SIGTERM, lambda sig, _frame: net.append(sig))
        try:
            running = asyncio.Event()

            async def scheduler_main() -> None:
                await scheduler_module._AppConfigScheduleSource().startup()
                running.set()
                await asyncio.sleep(30)

            task = asyncio.create_task(scheduler_main())
            await running.wait()

            os.kill(os.getpid(), signal.SIGTERM)

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert not net, "startup() installed no handler; the safety net took the signal"
        finally:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                asyncio.get_running_loop().remove_signal_handler(signal.SIGTERM)
            signal.signal(signal.SIGTERM, previous)

    @pytest.mark.asyncio
    async def test_the_handler_is_not_installed_merely_by_importing(
        self, scheduler_module: ModuleType
    ) -> None:
        """Which is why it lives in startup() rather than at module scope.

        This module is imported by the test suite as well, and a process-wide
        SIGTERM handler has no business appearing there.
        """
        loop = asyncio.get_running_loop()
        installed: list[int] = []
        real = loop.add_signal_handler

        def spy(sig: int, callback: Any, *args: Any) -> None:
            installed.append(sig)
            real(sig, callback, *args)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(loop, "add_signal_handler", spy)
            importlib.reload(scheduler_module)
            assert installed == [], "importing the module grabbed a signal"

            await scheduler_module._AppConfigScheduleSource().startup()

        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.remove_signal_handler(signal.SIGTERM)
        assert installed == [signal.SIGTERM]


# Run out of process. Several sibling files stub taskiq by writing attributes
# straight onto the real module objects (tests/tasks/github_sync_helpers.py and
# friends do `sys.modules["taskiq"].TaskiqMiddleware = object` with nothing to
# undo it), which permanently replaces real classes for the rest of the session.
# Verified: after tests/tasks/test_github_sync.py runs, taskiq.TaskiqMiddleware
# is `object`. Harmless in CI, which installs no taskiq at all, but it means an
# in-process version of this comparison passes alone and fails in the suite. A
# fresh interpreter is immune, and it exercises the same code either way.
_PROPAGATION_PROBE = """
import asyncio, json, os, sys
os.environ["OTEL_ENABLED"] = "true"
os.environ["OTEL_TRACES_EXPORTER"] = "console"
os.environ["TASKIQ_BROKER"] = "memory"

from app.observability.otel import init_tracing
init_tracing()

import taskiq
from taskiq.abc.middleware import TaskiqMiddleware
from taskiq.abc.schedule_source import ScheduleSource
from taskiq.scheduler.scheduled_task import ScheduledTask
from app.tasks.middleware import OTelPropagationMiddleware
from app.tasks.scheduler import _TracedScheduler

async def labels_from_a_kick(scheduler_cls):
    captured = {}

    class Capture(TaskiqMiddleware):
        async def pre_send(self, message):
            captured.update(message.labels)
            return message

    broker = taskiq.InMemoryBroker().with_middlewares(OTelPropagationMiddleware(), Capture())

    @broker.task(task_name="probe.tick")
    async def _tick() -> None: ...

    class Source(ScheduleSource):
        async def get_schedules(self): return []

    source = Source()
    await broker.startup()
    try:
        await scheduler_cls(broker=broker, sources=[source]).on_ready(
            source,
            ScheduledTask(task_name="probe.tick", cron="* * * * *",
                          labels={}, args=[], kwargs={}),
        )
    finally:
        await broker.shutdown()
    return captured

out = {
    "stock": asyncio.run(labels_from_a_kick(taskiq.TaskiqScheduler)),
    "traced": asyncio.run(labels_from_a_kick(_TracedScheduler)),
}
sys.stderr.write("RESULT" + json.dumps(out))
"""


class TestTheEnqueueCarriesTraceContext:
    def test_the_kick_now_has_a_span_to_propagate(self) -> None:
        pytest.importorskip("opentelemetry", reason="the otel extra is not installed")

        root = pathlib.Path(__file__).resolve().parents[2]
        finished = subprocess.run(
            [sys.executable, "-c", _PROPAGATION_PROBE],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        marker = finished.stderr.rfind("RESULT")
        assert marker != -1, f"the probe did not finish:\n{finished.stderr[-2000:]}"
        result = json.loads(finished.stderr[marker + len("RESULT") :])

        assert "traceparent" not in result["stock"], "this test no longer demonstrates anything"
        assert "traceparent" in result["traced"], (
            "the worker's task span is still an orphan; nothing links it to the schedule"
        )
