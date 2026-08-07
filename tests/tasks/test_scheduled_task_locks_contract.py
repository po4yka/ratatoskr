"""Every cron-scheduled task must serialize itself against its own previous run.

A scheduled task that outlives its interval overlaps with the next firing, and
these tasks are not idempotent: the RSS poll sends each message to Telegram
*before* marking the (user, item) pair delivered, and the topic-change watch
reads its "already delivered" watermark from a row it writes after sending. Two
runs therefore read the same ledger and send the same message twice.

Both had the gap while every other scheduled task held a lock, which is why this
test derives the set from the scheduler instead of listing it: a list would be
the same incomplete answer that let two tasks sit outside it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TASKS_DIR = ROOT / "app" / "tasks"

_LOCK_CLASS = "RedisDistributedLock"


def _task_name_to_module() -> dict[str, Path]:
    """Map every @broker.task(task_name=...) to the module that defines it.

    scheduler.py is excluded: it names the same tasks in its ScheduledTask
    entries, and letting those matches into the registry maps every task to the
    scheduler -- which holds no lock and never will, turning the assertions below
    into a uniform failure that says nothing about the tasks themselves.
    """
    registry: dict[str, Path] = {}
    for path in sorted(TASKS_DIR.glob("*.py")):
        if path.name == "scheduler.py":
            continue
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r'task_name\s*=\s*["\']([\w.]+)["\']', source):
            registry[match.group(1)] = path
    return registry


def _scheduled_task_names() -> set[str]:
    """Every task name the cron scheduler can fire."""
    scheduler_source = (TASKS_DIR / "scheduler.py").read_text(encoding="utf-8")
    return set(re.findall(r'task_name\s*=\s*["\']([\w.]+)["\']', scheduler_source))


_REGISTRY = _task_name_to_module()
_SCHEDULED = sorted(_scheduled_task_names())


def test_the_derivation_found_the_tasks() -> None:
    """A derivation that matches nothing would make every assertion below vacuous."""
    assert len(_SCHEDULED) >= 8, f"only found {_SCHEDULED} -- the scheduler scan is stale"
    for expected in ("ratatoskr.rss.poll", "ratatoskr.digest.run", "ratatoskr.jobs.reap"):
        assert expected in _SCHEDULED, f"{expected} is cron-scheduled and was not derived"


def test_every_scheduled_task_is_defined_somewhere() -> None:
    """A scheduled name with no @broker.task would enqueue messages nothing handles."""
    missing = [name for name in _SCHEDULED if name not in _REGISTRY]
    assert not missing, f"scheduled but never registered with @broker.task: {missing}"


@pytest.mark.parametrize("task_name", _SCHEDULED)
def test_scheduled_task_holds_a_distributed_lock(task_name: str) -> None:
    module = _REGISTRY[task_name]
    source = module.read_text(encoding="utf-8")

    assert _LOCK_CLASS in source, (
        f"{task_name} is cron-scheduled but {module.relative_to(ROOT)} never uses "
        f"{_LOCK_CLASS}, so a run that outlives its interval overlaps the next one"
    )


@pytest.mark.parametrize("task_name", _SCHEDULED)
def test_scheduled_task_returns_early_when_the_lock_is_held(task_name: str) -> None:
    """Acquiring is not enough -- a held lock has to short-circuit the body.

    ``RedisDistributedLock.__aenter__`` returns False rather than raising when
    another run holds the lock, so a task that ignores the bound value runs its
    body anyway and the lock buys nothing.
    """
    module = _REGISTRY[task_name]
    source = module.read_text(encoding="utf-8")

    bound = re.findall(rf"{_LOCK_CLASS}\(.*?\)\s*as\s+(\w+)\s*:", source, flags=re.DOTALL)
    assert bound, (
        f"{task_name} references {_LOCK_CLASS} but never binds its result with "
        "`as`, so the acquisition outcome cannot be checked at all"
    )
    for name in bound:
        assert re.search(rf"if\s+not\s+{name}\s*:", source), (
            f"{task_name} binds {_LOCK_CLASS} as {name!r} in "
            f"{module.relative_to(ROOT)} but never branches on it, so a second "
            "run proceeds as if it held the lock"
        )
