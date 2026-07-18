"""Apply Taskiq worker-only overrides to process-local runtime resources."""

from __future__ import annotations

import os
from dataclasses import replace
from typing import TYPE_CHECKING

from app.config.database import DatabaseConfig
from app.config.runtime import RuntimeConfig

if TYPE_CHECKING:
    from collections.abc import Mapping

    from app.config.settings import AppConfig


def _first_value(environ: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = environ.get(name)
        if value not in (None, ""):
            return value
    return None


def apply_worker_process_overrides(
    config: AppConfig,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Return config with explicit worker-only per-process limits applied.

    Normal non-secret settings come from ``ratatoskr.yaml``. These overrides
    intentionally run after that merge because one image serves the bot, API,
    and Taskiq worker while only the worker multiplies resources by child
    process count.
    """
    source = os.environ if environ is None else environ

    runtime_updates: dict[str, str] = {}
    max_async_tasks = _first_value(
        source,
        "TASKIQ_MAX_ASYNC_TASKS_PER_PROCESS",
        "URL_WORKER_CONCURRENCY",
    )
    if max_async_tasks is not None:
        runtime_updates["url_worker_concurrency"] = max_async_tasks

    max_external_calls = _first_value(
        source,
        "TASKIQ_MAX_CONCURRENT_CALLS_PER_PROCESS",
        "MAX_CONCURRENT_CALLS",
    )
    if max_external_calls is not None:
        runtime_updates["max_concurrent_calls"] = max_external_calls

    database_updates: dict[str, str] = {}
    pool_size = _first_value(
        source,
        "TASKIQ_DATABASE_POOL_SIZE_PER_PROCESS",
        "DATABASE_POOL_SIZE",
    )
    if pool_size is not None:
        database_updates["pool_size"] = pool_size

    max_overflow = _first_value(
        source,
        "TASKIQ_DATABASE_MAX_OVERFLOW_PER_PROCESS",
        "DATABASE_MAX_OVERFLOW",
    )
    if max_overflow is not None:
        database_updates["max_overflow"] = max_overflow

    runtime = config.runtime
    if runtime_updates:
        runtime = RuntimeConfig.model_validate(
            {**runtime.model_dump(mode="python"), **runtime_updates}
        )

    database = config.database
    if database_updates:
        database = DatabaseConfig.model_validate(
            {**database.model_dump(mode="python"), **database_updates}
        )

    return replace(config, runtime=runtime, database=database)
