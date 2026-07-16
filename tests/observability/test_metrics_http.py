from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("prometheus_client")

from prometheus_client import generate_latest

from app.observability.metrics_http import (
    build_multiprocess_registry,
    configured_metrics_port,
    prepare_multiprocess_directory,
    start_metrics_http_server_from_env,
)


def test_metrics_port_is_optional_and_validated() -> None:
    assert configured_metrics_port({}) is None
    assert configured_metrics_port({"METRICS_HTTP_PORT": "9102"}) == 9102

    with pytest.raises(ValueError, match="integer"):
        configured_metrics_port({"METRICS_HTTP_PORT": "metrics"})
    with pytest.raises(ValueError, match="between"):
        configured_metrics_port({"METRICS_HTTP_PORT": "0"})


def test_prepare_multiprocess_directory_only_clears_metric_files(tmp_path: Path) -> None:
    stale_metric = tmp_path / "counter_123.db"
    unrelated = tmp_path / "keep.txt"
    stale_metric.write_text("stale", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")

    result = prepare_multiprocess_directory(
        {"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    )

    assert result == tmp_path
    assert not stale_metric.exists()
    assert unrelated.exists()


def test_single_process_server_uses_app_registry(monkeypatch) -> None:
    from app.observability import metrics_http

    sentinel = object()
    calls = []
    monkeypatch.setattr(
        metrics_http,
        "start_http_server",
        lambda port, **kwargs: calls.append((port, kwargs)) or sentinel,
    )

    result = start_metrics_http_server_from_env(
        environ={"METRICS_HTTP_PORT": "9101", "RATATOSKR_PROCESS_ROLE": "bot"}
    )

    assert result is sentinel
    assert calls == [(9101, {"registry": metrics_http.REGISTRY})]
    payload = generate_latest(metrics_http.REGISTRY).decode("utf-8")
    assert 'ratatoskr_process_start_time_seconds{role="bot"}' in payload


def test_worker_registry_aggregates_metrics_from_all_child_processes(tmp_path: Path) -> None:
    prepare_multiprocess_directory({"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)})
    environment = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    script = """
from app.observability.metrics import (
    record_vector_reconcile_run,
    set_db_connections,
    set_vector_reconcile_oldest_lag_seconds,
)
record_vector_reconcile_run(status="success")
set_db_connections(VALUE)
set_vector_reconcile_oldest_lag_seconds(VALUE)
"""

    for value in (2, 3):
        subprocess.run(
            [sys.executable, "-c", script.replace("VALUE", str(value))],
            check=True,
            env=environment,
        )

    registry = build_multiprocess_registry(tmp_path)
    payload = generate_latest(registry).decode("utf-8")

    assert 'ratatoskr_vector_reconcile_runs_total{status="success"} 2.0' in payload
    assert "ratatoskr_vector_reconcile_oldest_lag_seconds 3.0" in payload
    assert "ratatoskr_db_connections_active" not in payload
    assert "pid=" not in payload
