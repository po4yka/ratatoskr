"""Tests for Taskiq retry metrics and dead-letter persistence middleware."""

from __future__ import annotations

import importlib
import sys
from typing import Any

import pytest

from app.db.models import ALL_MODELS, TaskiqFailedJob
from app.observability import metrics as metrics_module
from app.tasks.middleware import TaskiqDeadLetterMiddleware


class _FakeMessage:
    def __init__(
        self,
        *,
        labels: dict[str, Any] | None = None,
        task_name: str = "ratatoskr.test.task",
    ) -> None:
        self.task_name = task_name
        self.task_id = "task-123"
        self.labels = labels or {}
        self.args = [1, "two"]
        self.kwargs = {"hello": "world"}


class _FakeResult:
    def __init__(self, *, is_err: bool) -> None:
        self.is_err = is_err


def _counter_value(counter, **labels: str) -> float:
    if counter is None:
        return 0.0
    return float(counter.labels(**labels)._value.get())


def test_taskiq_failed_job_model_is_registered() -> None:
    assert TaskiqFailedJob in ALL_MODELS
    assert "kwargs_json" in TaskiqFailedJob.__table__.columns
    assert "traceback_text" in TaskiqFailedJob.__table__.columns
    assert "attempt_count" in TaskiqFailedJob.__table__.columns


def test_memory_broker_registers_retry_and_dead_letter_middlewares(monkeypatch) -> None:
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    original_broker_module = sys.modules.pop("app.tasks.broker", None)

    try:
        broker_module = importlib.import_module("app.tasks.broker")

        if type(broker_module.broker).__name__ == "MagicMock":
            pytest.skip("taskiq is stubbed in this test process")
        assert type(broker_module.broker).__name__ == "InMemoryBroker"
        assert [type(middleware).__name__ for middleware in broker_module.broker.middlewares] == [
            "SimpleRetryMiddleware",
            "ChronicFailureMiddleware",
            "TaskiqDeadLetterMiddleware",
            "OTelPropagationMiddleware",
        ]
    finally:
        sys.modules.pop("app.tasks.broker", None)
        if original_broker_module is not None:
            sys.modules["app.tasks.broker"] = original_broker_module


@pytest.mark.asyncio
async def test_retryable_failure_records_retry_metric_without_dlq() -> None:
    persisted: list[dict[str, Any]] = []

    async def _persist(**payload: Any) -> int:
        persisted.append(payload)
        return 1

    middleware = TaskiqDeadLetterMiddleware(persist_failed_job=_persist)
    message = _FakeMessage(labels={"retry_on_error": True, "max_retries": 3, "_retries": 0})
    before = _counter_value(
        metrics_module.TASKIQ_RETRIES_TOTAL,
        task="ratatoskr.test.task",
        outcome="retry",
    )

    await middleware.on_error(message, _FakeResult(is_err=True), RuntimeError("temporary"))

    assert persisted == []
    assert (
        _counter_value(
            metrics_module.TASKIQ_RETRIES_TOTAL,
            task="ratatoskr.test.task",
            outcome="retry",
        )
        == before + 1
    )


@pytest.mark.asyncio
async def test_terminal_failure_is_dead_lettered_with_payload() -> None:
    persisted: list[dict[str, Any]] = []

    async def _persist(**payload: Any) -> int:
        persisted.append(payload)
        return 42

    middleware = TaskiqDeadLetterMiddleware(persist_failed_job=_persist)
    message = _FakeMessage(labels={"retry_on_error": True, "max_retries": 3, "_retries": 2})
    before = _counter_value(
        metrics_module.TASKIQ_RETRIES_TOTAL,
        task="ratatoskr.test.task",
        outcome="dead_letter",
    )

    await middleware.on_error(message, _FakeResult(is_err=True), ValueError("permanent"))

    assert len(persisted) == 1
    payload = persisted[0]
    assert payload["task_name"] == "ratatoskr.test.task"
    assert payload["task_id"] == "task-123"
    assert payload["args"] == [1, "two"]
    assert payload["kwargs"] == {"hello": "world"}
    assert payload["attempt_count"] == 3
    assert "ValueError: permanent" in payload["traceback_text"]
    assert payload["error_text"] == "ValueError: permanent"
    assert (
        _counter_value(
            metrics_module.TASKIQ_RETRIES_TOTAL,
            task="ratatoskr.test.task",
            outcome="dead_letter",
        )
        == before + 1
    )


@pytest.mark.asyncio
async def test_success_after_retry_metric_records_on_recovered_message() -> None:
    middleware = TaskiqDeadLetterMiddleware()
    message = _FakeMessage(labels={"_retries": 1})
    before = _counter_value(
        metrics_module.TASKIQ_RETRIES_TOTAL,
        task="ratatoskr.test.task",
        outcome="success_after_retry",
    )

    result = await middleware.post_execute(message, _FakeResult(is_err=False))

    assert result.is_err is False
    assert (
        _counter_value(
            metrics_module.TASKIQ_RETRIES_TOTAL,
            task="ratatoskr.test.task",
            outcome="success_after_retry",
        )
        == before + 1
    )
