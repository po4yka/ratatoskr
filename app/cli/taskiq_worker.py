"""Start Taskiq with bounded, process-aware worker capacity."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING, Any

from app.config import load_config
from app.config.worker_capacity import apply_worker_process_overrides

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from app.config import AppConfig


def _worker_processes(environ: Mapping[str, str]) -> int:
    raw = environ.get("TASKIQ_WORKER_PROCESSES") or environ.get("TASKIQ_WORKER_CONCURRENCY", "1")
    try:
        processes = int(raw)
    except ValueError as exc:
        raise ValueError("TASKIQ_WORKER_PROCESSES must be an integer") from exc
    if not 1 <= processes <= 32:
        raise ValueError("TASKIQ_WORKER_PROCESSES must be between 1 and 32")
    return processes


def build_worker_command(
    config: AppConfig,
    modules: Sequence[str],
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Build the bounded Taskiq command from effective worker config."""
    processes = _worker_processes(os.environ if environ is None else environ)
    max_async_tasks = config.runtime.url_worker_concurrency
    return [
        "taskiq",
        "worker",
        "app.tasks.broker:broker",
        *modules,
        "--workers",
        str(processes),
        "--max-async-tasks",
        str(max_async_tasks),
        "--max-prefetch",
        str(max_async_tasks),
    ]


def capacity_summary(config: AppConfig, processes: int) -> str:
    """Format effective per-process and aggregate capacity for startup logs."""
    async_tasks = config.runtime.url_worker_concurrency
    external_calls = config.runtime.max_concurrent_calls
    db_connections = config.database.pool_size + config.database.max_overflow
    return (
        "Taskiq capacity: "
        f"processes={processes}; "
        f"async_tasks={async_tasks}/process ({processes * async_tasks} total); "
        f"external_calls={external_calls}/process ({processes * external_calls} total); "
        f"database_connections={db_connections}/process "
        f"({processes * db_connections} total)"
    )


def run_worker_with_metrics(command: Sequence[str]) -> int:
    """Run Taskiq under a signal-forwarding metrics supervisor."""
    from app.observability.metrics_http import (
        prepare_multiprocess_directory,
        start_metrics_http_server_from_env,
    )

    multiprocess_directory = prepare_multiprocess_directory()
    child = subprocess.Popen(command, start_new_session=True)

    handled_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        handled_signals.append(signal.SIGHUP)
    previous_handlers: dict[signal.Signals, Any] = {}

    def _forward_signal(signum: int, _frame: Any) -> None:
        if child.poll() is None:
            os.killpg(child.pid, signum)

    try:
        for handled_signal in handled_signals:
            previous_handlers[handled_signal] = signal.getsignal(handled_signal)
            signal.signal(handled_signal, _forward_signal)
        start_metrics_http_server_from_env(
            multiprocess_directory=multiprocess_directory,
        )
        return child.wait()
    except BaseException:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait()
        raise
    finally:
        for handled_signal, previous_handler in previous_handlers.items():
            signal.signal(handled_signal, previous_handler)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("modules", nargs="+", help="Task modules to import")
    args = parser.parse_args(argv)

    config = apply_worker_process_overrides(load_config())
    command = build_worker_command(config, args.modules)
    processes = int(command[command.index("--workers") + 1])
    print(capacity_summary(config, processes), file=sys.stderr, flush=True)
    if os.getenv("METRICS_HTTP_PORT"):
        status = run_worker_with_metrics(command)
        if status:
            raise SystemExit(status)
        return
    os.execvp(command[0], command)


if __name__ == "__main__":  # pragma: no cover
    main()
