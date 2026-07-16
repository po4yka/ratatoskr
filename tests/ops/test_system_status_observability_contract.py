from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


class _ComposeLoader(yaml.SafeLoader):
    """Load Compose merge tags as their underlying YAML values."""


def _construct_override(loader: _ComposeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    raise TypeError(f"Unsupported Compose override node: {type(node).__name__}")


_ComposeLoader.add_constructor("!override", _construct_override)


def _compose(filename: str = "docker-compose.yml") -> dict:
    return yaml.load(
        (ROOT / "ops/docker" / filename).read_text(encoding="utf-8"),
        Loader=_ComposeLoader,
    )


def _environment(service: dict) -> dict[str, str]:
    return dict(item.split("=", 1) for item in service.get("environment", []))


def _dashboard_expressions() -> list[str]:
    dashboard = json.loads(
        (
            ROOT / "ops/monitoring/grafana/provisioning/dashboards/ratatoskr-system-status.json"
        ).read_text(encoding="utf-8")
    )
    return [
        target["expr"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if target.get("expr")
    ]


def test_grafana_provisions_dashboard_prometheus_uid() -> None:
    datasources = yaml.safe_load(
        (
            ROOT / "ops/monitoring/grafana/provisioning/datasources/datasources.yml"
        ).read_text(encoding="utf-8")
    )
    prometheus = next(
        item for item in datasources["datasources"] if item["type"] == "prometheus"
    )

    assert prometheus["uid"] == "prometheus"


def test_status_process_metric_urls_belong_only_to_mobile_api() -> None:
    expected = {
        "STATUS_BOT_METRICS_URL": "http://ratatoskr:9101/metrics",
        "STATUS_WORKER_METRICS_URL": "http://worker:9102/metrics",
        "STATUS_SCHEDULER_METRICS_URL": "http://scheduler:9103/metrics",
    }

    for filename in ("docker-compose.yml", "docker-compose.pi.yml"):
        services = _compose(filename)["services"]
        assert {key: _environment(services["mobile-api"])[key] for key in expected} == expected
        for name in ("ratatoskr", "worker", "scheduler"):
            if name in services:
                assert not set(expected) & _environment(services[name]).keys()


def test_api_production_surfaces_use_metrics_aware_launcher() -> None:
    services = _compose()["services"]
    api = services["mobile-api"]
    environment = _environment(api)
    command = "\n".join(api["command"])
    dockerfile = (ROOT / "ops/docker/Dockerfile.api").read_text(encoding="utf-8")

    assert environment["PROMETHEUS_MULTIPROC_DIR"] == "/tmp/prometheus-api"
    assert "exec python -m app.cli.api_server" in command
    assert "uvicorn app.api.main:app" not in command
    assert 'CMD ["python", "-m", "app.cli.api_server"]' in dockerfile
    assert "PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus-api" in dockerfile
    assert "exec uvicorn app.api.main:app" not in dockerfile
    assert (
        _environment(_compose("docker-compose.pi.yml")["services"]["mobile-api"])[
            "PROMETHEUS_MULTIPROC_DIR"
        ]
        == "/tmp/prometheus-api"
    )


def test_dependency_exporters_are_pinned_internal_and_bounded() -> None:
    services = _compose()["services"]

    expected = {
        "postgres-exporter": (
            "9187",
            "postgres",
            "quay.io/prometheuscommunity/postgres-exporter:v0.20.1@sha256:"
            "ac5ec343104fae0e2d84a27bb8d69b38430a11910c5382cad85d478d2bab713e",
        ),
        "redis-exporter": (
            "9121",
            "redis",
            "oliver006/redis_exporter:v1.87.0-alpine@sha256:"
            "1a286dba9547b0aa3ebd4e4106fa52ad67c754dcd7cb744eb745e41d48b252ad",
        ),
    }
    for name, (port, dependency, image) in expected.items():
        service = services[name]
        assert service["image"] == image
        assert service["profiles"] == ["with-monitoring"]
        assert service["expose"] == [port]
        assert "ports" not in service
        assert service["read_only"] is True
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["depends_on"][dependency]["condition"] == "service_healthy"
        assert service["healthcheck"]["test"][-1].endswith(f":{port}/metrics")
        assert service["deploy"]["resources"]["limits"]["memory"] == "64M"


def test_prometheus_scrapes_every_dependency_target() -> None:
    config = yaml.safe_load((ROOT / "ops/monitoring/prometheus.yml").read_text(encoding="utf-8"))
    jobs = {
        item["job_name"]: item["static_configs"][0]["targets"] for item in config["scrape_configs"]
    }

    assert jobs["postgres"] == ["postgres-exporter:9187"]
    assert jobs["redis"] == ["redis-exporter:9121"]
    assert jobs["qdrant"] == ["qdrant:6333"]
    assert jobs["node"] == ["node-exporter:9100"]


def test_separate_monitoring_stack_joins_existing_core_network() -> None:
    compose = _compose("docker-compose.monitoring.yml")
    services = compose["services"]

    assert compose["networks"]["app"] == {
        "external": True,
        "name": "${RATATOSKR_DOCKER_NETWORK:-docker_default}",
    }
    assert compose["volumes"]["pg-backup-metrics"] == {
        "external": True,
        "name": "${RATATOSKR_PG_BACKUP_METRICS_VOLUME:-docker_pg_backup_metrics}",
    }
    for name in ("prometheus", "postgres-exporter", "redis-exporter"):
        assert "app" in services[name]["networks"]
    primary_services = _compose()["services"]
    for name in ("postgres-exporter", "redis-exporter"):
        assert services[name]["image"] == primary_services[name]["image"]
        assert services[name]["expose"] == primary_services[name]["expose"]
        assert "ports" not in services[name]
    assert "postgres" not in services["postgres-exporter"].get("depends_on", {})
    assert "redis" not in services["redis-exporter"].get("depends_on", {})
    node_exporter = services["node-exporter"]
    assert "--collector.textfile.directory=/textfile" in node_exporter["command"]
    assert "pg-backup-metrics:/textfile:ro" in node_exporter["volumes"]


def test_pi_prometheus_scrapes_native_qdrant_through_host_gateway() -> None:
    prometheus = _compose("docker-compose.pi.yml")["services"]["prometheus"]

    assert prometheus["extra_hosts"] == ["qdrant:host-gateway"]


def test_system_status_dashboard_covers_real_operational_families() -> None:
    expressions = "\n".join(_dashboard_expressions())

    required = {
        "ratatoskr_http_requests_total",
        "ratatoskr_http_request_duration_seconds_bucket",
        "ratatoskr_http_requests_in_flight",
        "ratatoskr_requests_total",
        "ratatoskr_request_latency_seconds_bucket",
        "ratatoskr_url_processing_queue_depth",
        "ratatoskr_url_processor_in_flight",
        "ratatoskr_taskiq_retries_total",
        "ratatoskr_taskiq_executions_total",
        "ratatoskr_taskiq_execution_duration_seconds_bucket",
        "ratatoskr_taskiq_in_flight",
        "ratatoskr_scraper_attempts_total",
        "ratatoskr_llm_call_attempts_total",
        "ratatoskr_digest_deliveries_total",
        "ratatoskr_github_sync_runs_total",
        "ratatoskr_social_fetch_total",
        "ratatoskr_status_checks_total",
        "ratatoskr_status_check_duration_seconds_bucket",
        "ratatoskr_status_component_state",
        "ratatoskr_vector_reconcile_oldest_lag_seconds",
        "pg_up",
        "pg_stat_database_numbackends",
        "redis_up",
        "redis_memory_used_bytes",
        "node_cpu_seconds_total",
        "ratatoskr_pg_backup_last_success_timestamp_seconds",
    }
    assert all(metric in expressions for metric in required)

    assert (
        "sum by (route, method, status_class) (rate(ratatoskr_http_requests_total[5m]))"
        in expressions
    )
    assert "sum by (method) (ratatoskr_http_requests_in_flight)" in expressions
    assert (
        "sum by (le, route, method) "
        "(rate(ratatoskr_http_request_duration_seconds_bucket[5m]))" in expressions
    )
    assert (
        "sum by (task, outcome) (rate(ratatoskr_taskiq_executions_total[15m]))" in expressions
    )
    assert "sum by (task) (ratatoskr_taskiq_in_flight)" in expressions
    assert (
        "sum by (le, task) (rate(ratatoskr_taskiq_execution_duration_seconds_bucket[15m]))"
        in expressions
    )
    assert "sum by (component, status) (rate(ratatoskr_status_checks_total[5m]))" in expressions
    assert (
        "sum by (le, component) (rate(ratatoskr_status_check_duration_seconds_bucket[5m]))"
        in expressions
    )
    assert "max by (component) (ratatoskr_status_component_state)" in expressions

    # This gauge is declared for compatibility but has no production call site.
    assert "ratatoskr_scheduler_queue_depth" not in expressions


def test_dependency_and_host_alerts_use_exported_metric_families() -> None:
    rules = yaml.safe_load((ROOT / "ops/monitoring/alerting_rules.yml").read_text(encoding="utf-8"))
    alerts = {
        rule["alert"]: rule["expr"]
        for group in rules["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }

    assert 'up{job=~"postgres|redis|qdrant|node"} == 0' in alerts["RatatoskrDependencyMetricsDown"]
    assert "pg_up" in alerts["RatatoskrPostgresUnavailable"]
    assert "redis_up" in alerts["RatatoskrRedisUnavailable"]
    assert "ratatoskr_status_component_state" in alerts["RatatoskrStatusComponentOutage"]
    assert "ratatoskr_http_requests_total" in alerts["RatatoskrAPIHigh5xxRate"]
    assert "ratatoskr_taskiq_executions_total" in alerts["RatatoskrTaskiqExecutionErrorRateHigh"]
    assert "pg_stat_database_numbackends" in alerts["RatatoskrPostgresConnectionsHigh"]
    assert "redis_evicted_keys_total" in alerts["RatatoskrRedisEvictionsDetected"]
    assert "node_cpu_seconds_total" in alerts["RatatoskrHostCPUHigh"]
    assert "node_memory_MemAvailable_bytes" in alerts["RatatoskrHostMemoryLow"]
    assert "node_filesystem_avail_bytes" in alerts["RatatoskrHostDiskSpaceLow"]
