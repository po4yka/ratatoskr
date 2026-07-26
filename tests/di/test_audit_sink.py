"""build_async_audit_sink must strongly reference its fire-and-forget tasks.

asyncio only weakly references tasks, so an unreferenced create_task() result can
be garbage-collected before the audit DB write completes. Most call sites do not
pass a task_registry, so the sink must fall back to a process-wide registry that
holds a strong reference until the write finishes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.di import shared


class _FakeRepo:
    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.calls: list[dict[str, Any]] = []
        self.started = asyncio.Event()

    async def async_insert_audit_log(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()


@pytest.fixture(autouse=True)
def _clear_default_registry():
    shared._AUDIT_TASKS.clear()
    yield
    shared._AUDIT_TASKS.clear()


@pytest.mark.asyncio
async def test_audit_sink_without_registry_holds_strong_reference(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = asyncio.Event()
    repo = _FakeRepo(gate)
    monkeypatch.setattr(shared, "build_audit_log_repository", lambda _db: repo)

    audit = shared.build_async_audit_sink(object())
    audit("info", "evt", {"k": "v"})

    # While the write is in flight it must be strongly referenced, or it could be
    # GC'd before completing (the bug this guards against).
    await repo.started.wait()
    tasks = list(shared._AUDIT_TASKS)
    assert len(tasks) == 1

    gate.set()
    await asyncio.gather(*tasks)
    await asyncio.sleep(0)  # let the done-callback discard run

    assert repo.calls == [{"log_level": "info", "event_type": "evt", "details": {"k": "v"}}]
    # Completed tasks are discarded, so the registry stays bounded.
    assert not shared._AUDIT_TASKS


@pytest.mark.asyncio
async def test_audit_sink_prefers_caller_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepo()
    monkeypatch.setattr(shared, "build_audit_log_repository", lambda _db: repo)

    caller_registry: set[asyncio.Task[Any]] = set()
    audit = shared.build_async_audit_sink(object(), task_registry=caller_registry)
    audit("error", "evt", {})

    # The explicit registry is used; the module-level default is untouched.
    assert len(caller_registry) == 1
    assert not shared._AUDIT_TASKS

    await asyncio.gather(*list(caller_registry))
    await asyncio.sleep(0)
    assert not caller_registry


@pytest.mark.asyncio
async def test_audit_sink_coerces_non_dict_details(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeRepo()
    monkeypatch.setattr(shared, "build_audit_log_repository", lambda _db: repo)

    audit = shared.build_async_audit_sink(object())
    audit("info", "evt", "not-a-dict")  # type: ignore[arg-type]

    await asyncio.gather(*list(shared._AUDIT_TASKS))
    await asyncio.sleep(0)
    assert repo.calls == [
        {"log_level": "info", "event_type": "evt", "details": {"details": "not-a-dict"}}
    ]
