from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import ValidationError

from app.cli.taskiq_worker import build_worker_command, capacity_summary
from app.config.database import DatabaseConfig
from app.config.runtime import RuntimeConfig
from app.config.worker_capacity import apply_worker_process_overrides


@dataclass(frozen=True)
class _Config:
    runtime: RuntimeConfig
    database: DatabaseConfig


def _config() -> _Config:
    return _Config(
        runtime=RuntimeConfig(),
        database=DatabaseConfig(
            dsn="postgresql+asyncpg://test:test@localhost:5432/test",
            pool_size=5,
            max_overflow=3,
        ),
    )


def test_worker_overrides_replace_yaml_derived_process_local_limits() -> None:
    config = apply_worker_process_overrides(
        _config(),  # type: ignore[arg-type]
        {
            "TASKIQ_MAX_ASYNC_TASKS_PER_PROCESS": "2",
            "TASKIQ_MAX_CONCURRENT_CALLS_PER_PROCESS": "3",
            "TASKIQ_DATABASE_POOL_SIZE_PER_PROCESS": "4",
            "TASKIQ_DATABASE_MAX_OVERFLOW_PER_PROCESS": "1",
        },
    )

    assert config.runtime.url_worker_concurrency == 2
    assert config.runtime.max_concurrent_calls == 3
    assert config.database.pool_size == 4
    assert config.database.max_overflow == 1


def test_worker_override_reuses_model_validation() -> None:
    with pytest.raises(ValidationError):
        apply_worker_process_overrides(
            _config(),  # type: ignore[arg-type]
            {"TASKIQ_MAX_ASYNC_TASKS_PER_PROCESS": "100"},
        )


def test_taskiq_command_bounds_async_tasks_and_prefetch() -> None:
    config = apply_worker_process_overrides(
        _config(),  # type: ignore[arg-type]
        {"TASKIQ_MAX_ASYNC_TASKS_PER_PROCESS": "3"},
    )

    command = build_worker_command(
        config,  # type: ignore[arg-type]
        ["app.tasks.url_processing"],
        {"TASKIQ_WORKER_PROCESSES": "2"},
    )

    assert command == [
        "taskiq",
        "worker",
        "app.tasks.broker:broker",
        "app.tasks.url_processing",
        "--workers",
        "2",
        "--max-async-tasks",
        "3",
        "--max-prefetch",
        "3",
    ]
    assert "async_tasks=3/process (6 total)" in capacity_summary(config, 2)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["0", "33", "many"])
def test_taskiq_process_count_is_validated(value: str) -> None:
    with pytest.raises(ValueError, match="TASKIQ_WORKER_PROCESSES"):
        build_worker_command(
            _config(),  # type: ignore[arg-type]
            ["app.tasks.url_processing"],
            {"TASKIQ_WORKER_PROCESSES": value},
        )
