"""Internal Prometheus HTTP exposition for non-API processes."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger
from app.observability._metrics_base import PROMETHEUS_AVAILABLE, REGISTRY

logger = get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

if PROMETHEUS_AVAILABLE:
    from prometheus_client import CollectorRegistry, Gauge, multiprocess, start_http_server

    PROCESS_START_TIME_SECONDS = Gauge(
        "ratatoskr_process_start_time_seconds",
        "Unix timestamp when a Ratatoskr service process started",
        ["role"],
        multiprocess_mode="max",
        registry=REGISTRY,
    )
else:
    PROCESS_START_TIME_SECONDS = None


def configured_metrics_port(environ: Mapping[str, str] | None = None) -> int | None:
    """Return the configured internal metrics port, if exposition is enabled."""
    source = os.environ if environ is None else environ
    raw = source.get("METRICS_HTTP_PORT", "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError as exc:
        raise ValueError("METRICS_HTTP_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("METRICS_HTTP_PORT must be between 1 and 65535")
    return port


def prepare_multiprocess_directory(
    environ: Mapping[str, str] | None = None,
) -> Path:
    """Create and clear the worker-local Prometheus multiprocess directory."""
    source = os.environ if environ is None else environ
    raw = source.get("PROMETHEUS_MULTIPROC_DIR", "").strip()
    if not raw:
        raise ValueError("PROMETHEUS_MULTIPROC_DIR is required for worker metrics")

    directory = Path(raw)
    if not directory.is_absolute():
        raise ValueError("PROMETHEUS_MULTIPROC_DIR must be an absolute path")
    directory.mkdir(parents=True, exist_ok=True)
    for metric_file in directory.glob("*.db"):
        metric_file.unlink()
    return directory


def configured_multiprocess_directory(
    environ: Mapping[str, str] | None = None,
) -> Path | None:
    """Return the configured multiprocess directory without mutating it."""
    source = os.environ if environ is None else environ
    raw = source.get("PROMETHEUS_MULTIPROC_DIR", "").strip()
    if not raw:
        return None
    directory = Path(raw)
    if not directory.is_absolute():
        raise ValueError("PROMETHEUS_MULTIPROC_DIR must be an absolute path")
    return directory


def _reap_dead_worker_gauges(directory: Path) -> None:
    """Remove live-gauge files for Taskiq children that no longer exist."""
    if not PROMETHEUS_AVAILABLE:
        return
    dead_pids: set[int] = set()
    for metric_file in directory.glob("gauge_live*.db"):
        try:
            pid = int(metric_file.stem.rsplit("_", 1)[1])
            os.kill(pid, 0)
        except ProcessLookupError:
            dead_pids.add(pid)
        except (IndexError, PermissionError, ValueError):
            continue
    for pid in dead_pids:
        multiprocess.mark_process_dead(pid, path=str(directory))


class _DeadWorkerGaugeCollector:
    """Collector hook that reaps stale live gauges before every scrape."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def collect(self) -> list[Any]:
        _reap_dead_worker_gauges(self._directory)
        return []


def build_multiprocess_registry(directory: Path) -> Any:
    """Build a worker registry that aggregates children and reaps live gauges."""
    if not PROMETHEUS_AVAILABLE:
        raise RuntimeError("prometheus_client is required for multiprocess metrics")
    registry = CollectorRegistry()
    registry.register(_DeadWorkerGaugeCollector(directory))
    multiprocess.MultiProcessCollector(registry, path=str(directory))
    return registry


def mark_process_dead(
    *,
    pid: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Mark one gracefully exiting child dead for live-gauge cleanup."""
    directory = configured_multiprocess_directory(environ)
    if directory is None or not PROMETHEUS_AVAILABLE:
        return False
    multiprocess.mark_process_dead(pid or os.getpid(), path=str(directory))
    return True


def start_metrics_http_server_from_env(
    *,
    multiprocess_directory: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> Any | None:
    """Start an internal metrics server when ``METRICS_HTTP_PORT`` is set."""
    source = os.environ if environ is None else environ
    port = configured_metrics_port(source)
    if port is None:
        return None
    if not PROMETHEUS_AVAILABLE:
        raise RuntimeError("prometheus_client is required when METRICS_HTTP_PORT is set")

    registry = REGISTRY
    if multiprocess_directory is not None:
        registry = build_multiprocess_registry(multiprocess_directory)

    role = source.get("RATATOSKR_PROCESS_ROLE", "unknown").strip() or "unknown"
    if PROCESS_START_TIME_SECONDS is not None:
        PROCESS_START_TIME_SECONDS.labels(role=role).set(time.time())

    server = start_http_server(port, registry=registry)
    logger.info(
        "metrics_http_server_started",
        extra={"port": port, "role": role, "multiprocess": multiprocess_directory is not None},
    )
    return server
