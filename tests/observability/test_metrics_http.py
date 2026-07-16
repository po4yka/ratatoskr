from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("prometheus_client")

from prometheus_client import generate_latest

from app.observability.metrics_http import (
    build_multiprocess_registry,
    configured_multiprocess_directory,
    configured_metrics_port,
    mark_process_dead,
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

    result = prepare_multiprocess_directory({"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)})

    assert result == tmp_path
    assert not stale_metric.exists()
    assert unrelated.exists()


def test_multiprocess_directory_is_optional_and_must_be_absolute(tmp_path: Path) -> None:
    assert configured_multiprocess_directory({}) is None
    assert (
        configured_multiprocess_directory({"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}) == tmp_path
    )

    with pytest.raises(ValueError, match="absolute path"):
        configured_multiprocess_directory({"PROMETHEUS_MULTIPROC_DIR": "relative"})


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


def test_api_scrape_aggregates_workers_and_reaps_dead_live_gauges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_multiprocess_directory({"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)})
    environment = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    script = """
from app.observability.metrics_http_requests import (
    change_http_in_flight,
    record_http_request,
)
record_http_request(
    route="/v1/status",
    method="GET",
    status_code=200,
    duration_seconds=0.01,
)
change_http_in_flight("GET", 1)
"""
    for _ in range(2):
        subprocess.run([sys.executable, "-c", script], check=True, env=environment)

    monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
    from app.observability.metrics import get_metrics

    first = get_metrics().decode("utf-8")
    second = get_metrics().decode("utf-8")

    expected = (
        'ratatoskr_http_requests_total{method="GET",route="/v1/status",status_class="2xx"} 2.0'
    )
    assert expected in first
    assert expected in second
    assert "ratatoskr_http_requests_in_flight" not in first
    assert not list(tmp_path.glob("gauge_live*.db"))


def test_graceful_process_exit_marks_live_gauges_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.observability import metrics_http

    calls: list[tuple[int, str]] = []
    monkeypatch.setattr(
        metrics_http.multiprocess,
        "mark_process_dead",
        lambda pid, *, path: calls.append((pid, path)),
    )

    assert mark_process_dead(
        pid=123,
        environ={"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)},
    )
    assert calls == [(123, str(tmp_path))]


def test_status_snapshot_uses_latest_live_api_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare_multiprocess_directory({"PROMETHEUS_MULTIPROC_DIR": str(tmp_path)})
    environment = {**os.environ, "PROMETHEUS_MULTIPROC_DIR": str(tmp_path)}
    script = """
import sys
import time
from app.observability.metrics_status import record_status_check
record_status_check("api", sys.argv[1], 0.01)
print("ready", flush=True)
time.sleep(30)
"""
    processes: list[subprocess.Popen[str]] = []
    try:
        for status in ("operational", "degraded"):
            process = subprocess.Popen(
                [sys.executable, "-c", script, status],
                env=environment,
                stdout=subprocess.PIPE,
                text=True,
            )
            processes.append(process)
            assert process.stdout is not None
            assert process.stdout.readline().strip() == "ready"
            if status == "operational":
                time.sleep(0.05)

        monkeypatch.setenv("PROMETHEUS_MULTIPROC_DIR", str(tmp_path))
        from app.observability.metrics import get_metrics

        payload = get_metrics().decode("utf-8")
        assert 'ratatoskr_status_component_state{component="api"} 2.0' in payload
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            process.wait(timeout=5)

    payload_after_exit = get_metrics().decode("utf-8")
    assert 'ratatoskr_status_component_state{component="api"}' not in payload_after_exit
