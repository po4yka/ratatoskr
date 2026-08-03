"""Faking taskiq for one test must not fake it for the rest of the session.

Eleven test files stub taskiq so the task modules import without Redis. They
inserted placeholder modules through monkeypatch, which is undone, but then
wrote the fakes straight onto whatever module object was in ``sys.modules`` --
and where taskiq is actually installed, that is the real package. Nothing undid
those writes. After ``tests/tasks/test_github_sync.py`` ran,
``taskiq.TaskiqMiddleware`` was ``object`` and ``taskiq.TaskiqScheduler`` was
``MagicMock`` for every later test in the process.

Seven symbols were left faked. The visible damage: middlewares built against a
faked base are not ``TaskiqMiddleware`` subclasses, so ``with_middlewares``
skipped all four of Ratatoskr's with a warning nobody reads, and
``tests/tasks/test_taskiq_retry_dlq.py`` passed alone while failing in the
suite. ``test_credential_refresh_lifecycle.py`` carried a workaround that
re-imported the entire taskiq package to get out from under it.

None of this showed in CI, which installs no ``scheduler`` extra at all, so the
placeholders are the only taskiq there is. That is why the check below runs the
offending file in a real interpreter rather than asserting on source text: the
property is about what survives a test, and only running one can show it.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("taskiq", reason="the scheduler extra is not installed")

ROOT = pathlib.Path(__file__).resolve().parents[2]

# One per stubbing style: the shared helper, a module-scope reloader, and the
# file whose helper had to be repaired by hand.
STUBBING_FILES = [
    "tests/tasks/test_github_sync.py",
    "tests/tasks/test_schedule_builder.py",
    "tests/tasks/test_url_processing_task.py",
    "tests/cli/test_sync_github_stars_cli.py",
]

_PROBE = textwrap.dedent("""
    import json, sys
    from unittest.mock import MagicMock
    import pytest

    pytest.main(["-q", "-p", "no:randomly", "--no-header", "--tb=no", *sys.argv[1:]])

    import taskiq, taskiq_redis
    import taskiq.abc.schedule_source as source_mod
    import taskiq.scheduler.scheduled_task as task_mod
    import taskiq.message as message_mod

    subjects = {
        "taskiq.TaskiqMiddleware": taskiq.TaskiqMiddleware,
        "taskiq.TaskiqScheduler": taskiq.TaskiqScheduler,
        "taskiq.AsyncBroker": taskiq.AsyncBroker,
        "taskiq.InMemoryBroker": taskiq.InMemoryBroker,
        "ScheduleSource": source_mod.ScheduleSource,
        "ScheduledTask": task_mod.ScheduledTask,
        "TaskiqMessage": message_mod.TaskiqMessage,
        "RedisStreamBroker": taskiq_redis.RedisStreamBroker,
    }
    # A fake is `object`, a MagicMock, or anything that no longer comes from the
    # package it is supposed to come from.
    faked = sorted(
        name for name, value in subjects.items()
        if value is object
        or value is MagicMock
        or not str(getattr(value, "__module__", "")).startswith("taskiq")
    )
    sys.stderr.write("RESULT" + json.dumps(faked))
""")


def test_the_real_taskiq_survives_the_stubs() -> None:
    """One interpreter for all of them: the question is what is left at the end."""
    finished = subprocess.run(
        [sys.executable, "-c", _PROBE, *STUBBING_FILES],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    marker = finished.stderr.rfind("RESULT")
    assert marker != -1, f"the probe did not finish:\n{finished.stderr[-2000:]}"

    faked = json.loads(finished.stderr[marker + len("RESULT") :])
    assert faked == [], (
        f"{sorted(STUBBING_FILES)} left {faked} faked for the rest of the session. "
        f"Route the stub writes through monkeypatch.setattr(..., raising=False)."
    )
