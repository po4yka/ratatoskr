from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PI_DEPLOY_SCRIPT = ROOT / "tools/scripts/build-and-deploy-pi.sh"
RUNTIME_DOCKERFILES = (
    ROOT / "ops/docker/Dockerfile",
    ROOT / "ops/docker/Dockerfile.api",
)


def _compose() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "ops/docker/docker-compose.yml").read_text(encoding="utf-8"))


def _dev_compose() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "ops/docker/docker-compose.dev.yml").read_text(encoding="utf-8"))


def _pi_deploy_script() -> str:
    return PI_DEPLOY_SCRIPT.read_text(encoding="utf-8")


def _env_map(service: dict[str, Any]) -> dict[str, str]:
    environment = service.get("environment", {})
    if isinstance(environment, dict):
        return {str(key): str(value) for key, value in environment.items()}
    result: dict[str, str] = {}
    for item in environment:
        key, _, value = str(item).partition("=")
        result[key] = value
    return result


def test_runtime_dockerfiles_do_not_duplicate_venv_for_app_ownership() -> None:
    for path in RUNTIME_DOCKERFILES:
        dockerfile = path.read_text(encoding="utf-8")

        assert "COPY --from=builder /app/.venv /app/.venv" in dockerfile
        assert "chown -R appuser:appuser /app" not in dockerfile
        assert "install -d -o appuser -g appuser /data" in dockerfile


def test_default_compose_stack_contains_core_services_without_profiles() -> None:
    services = _compose()["services"]

    for name in ("ratatoskr", "mobile-api", "redis", "qdrant"):
        assert name in services
        assert "profiles" not in services[name]

    assert services["mobile-api"]["ports"] == ["127.0.0.1:18000:8000"]


def test_mobile_api_healthcheck_uses_real_readiness_route() -> None:
    healthcheck = _compose()["services"]["mobile-api"]["healthcheck"]
    command = " ".join(healthcheck["test"])

    assert "/health/ready" in command
    assert "/healthz" not in command

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "$(PI_SMOKE_PORT)/web/" not in makefile
    assert "$(PI_SMOKE_PORT)/ 2>/dev/null" in makefile


def test_scrapers_profile_uses_internal_services_not_host_gateway() -> None:
    services = _compose()["services"]

    assert "extra_hosts" not in services["ratatoskr"]
    ratatoskr_env = _env_map(services["ratatoskr"])
    assert (
        ratatoskr_env["FIRECRAWL_SELF_HOSTED_URL"]
        == "${FIRECRAWL_SELF_HOSTED_URL:-http://firecrawl-api:3002}"
    )

    for name in (
        "firecrawl-api",
        "firecrawl-playwright",
        "firecrawl-redis",
        "firecrawl-rabbitmq",
        "firecrawl-postgres",
    ):
        assert name in services
        assert services[name]["profiles"] == ["with-scrapers"]

    assert services["firecrawl-api"]["depends_on"]["firecrawl-playwright"]["condition"]
    assert "3002" in services["firecrawl-api"]["ports"][0]


def test_cloud_ollama_profile_does_not_start_local_ollama() -> None:
    services = _compose()["services"]

    ollama_services = [name for name in services if "ollama" in name]
    assert ollama_services == ["cloud-ollama-check"]
    assert services["cloud-ollama-check"]["profiles"] == ["with-cloud-ollama"]

    ratatoskr_env = _env_map(services["ratatoskr"])
    assert ratatoskr_env["LLM_PROVIDER"] == "${LLM_PROVIDER:-openrouter}"
    assert ratatoskr_env["OLLAMA_BASE_URL"] == "${OLLAMA_BASE_URL:-http://localhost:11434/v1}"


def test_monitoring_profile_is_in_primary_compose_file() -> None:
    services = _compose()["services"]

    for name in ("alertmanager", "prometheus", "grafana", "loki", "promtail", "node-exporter"):
        assert name in services
        assert services[name]["profiles"] == ["with-monitoring"]


def test_prometheus_scrapes_every_application_process() -> None:
    services = _compose()["services"]
    prometheus_config = yaml.safe_load(
        (ROOT / "ops/monitoring/prometheus.yml").read_text(encoding="utf-8")
    )
    jobs = {
        job["job_name"]: job["static_configs"][0]["targets"]
        for job in prometheus_config["scrape_configs"]
    }

    assert jobs["ratatoskr-mobile-api"] == ["mobile-api:8000"]
    assert jobs["ratatoskr-bot"] == ["ratatoskr:9101"]
    assert jobs["ratatoskr-worker"] == ["worker:9102"]
    assert jobs["ratatoskr-scheduler"] == ["scheduler:9103"]

    assert _env_map(services["ratatoskr"])["METRICS_HTTP_PORT"] == "9101"
    assert services["ratatoskr"]["expose"] == ["9101"]
    assert "ports" not in services["ratatoskr"]
    worker_env = _env_map(services["worker"])
    assert worker_env["METRICS_HTTP_PORT"] == "9102"
    assert worker_env["PROMETHEUS_MULTIPROC_DIR"] == "/tmp/prometheus-worker"
    assert services["worker"]["expose"] == ["9102"]
    assert "ports" not in services["worker"]
    assert _env_map(services["scheduler"])["METRICS_HTTP_PORT"] == "9103"
    assert services["scheduler"]["expose"] == ["9103"]
    assert "ports" not in services["scheduler"]

    alerting = yaml.safe_load(
        (ROOT / "ops/monitoring/alerting_rules.yml").read_text(encoding="utf-8")
    )
    rules = [rule for group in alerting["groups"] for rule in group["rules"]]
    process_alert = next(
        rule for rule in rules if rule.get("alert") == "RatatoskrApplicationProcessDown"
    )
    assert 'up{job=~"ratatoskr-(mobile-api|bot|worker|scheduler)"} == 0' in process_alert["expr"]


def test_monitoring_alertmanager_routes_prometheus_and_loki_alerts() -> None:
    services = _compose()["services"]
    prometheus = services["prometheus"]
    loki = services["loki"]
    alertmanager = services["alertmanager"]

    assert alertmanager["image"] == "prom/alertmanager:v0.27.0"
    assert (
        "../monitoring/alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro"
        in alertmanager["volumes"]
    )
    assert (
        "../monitoring/render-alertmanager-config.sh:/etc/alertmanager/render-alertmanager-config.sh:ro"
        in alertmanager["volumes"]
    )
    assert alertmanager["entrypoint"] == [
        "/bin/sh",
        "/etc/alertmanager/render-alertmanager-config.sh",
    ]
    assert "alertmanager_data:/alertmanager" in alertmanager["volumes"]
    assert prometheus["depends_on"]["alertmanager"]["condition"] == "service_healthy"
    assert loki["depends_on"]["alertmanager"]["condition"] == "service_healthy"

    prometheus_config = yaml.safe_load(
        (ROOT / "ops/monitoring/prometheus.yml").read_text(encoding="utf-8")
    )
    loki_config = yaml.safe_load(
        (ROOT / "ops/monitoring/loki-config.yml").read_text(encoding="utf-8")
    )
    alertmanager_config = yaml.safe_load(
        (ROOT / "ops/monitoring/alertmanager.yml").read_text(encoding="utf-8")
    )

    targets = prometheus_config["alerting"]["alertmanagers"][0]["static_configs"][0]["targets"]
    assert targets == ["alertmanager:9093"]
    assert loki_config["ruler"]["alertmanager_url"] == "http://alertmanager:9093"
    assert alertmanager_config["route"]["receiver"] == "configured"
    assert alertmanager_config["receivers"] == [{"name": "configured"}]

    alertmanager_env = _env_map(alertmanager)
    assert set(
        {
            "ALERT_WEBHOOK_URL",
            "ALERT_SLACK_API_URL",
            "ALERT_TELEGRAM_WEBHOOK_URL",
            "ALERT_PAGERDUTY_ROUTING_KEY",
        }
    ).issubset(alertmanager_env)


def test_postgres_backup_sidecar_runs_in_default_compose_stack() -> None:
    services = _compose()["services"]
    pg_backup = services["pg-backup"]
    env = _env_map(pg_backup)

    assert "profiles" not in pg_backup
    assert pg_backup["build"]["dockerfile"] == "ops/docker/pg-backup/Dockerfile"
    assert pg_backup["depends_on"]["postgres"]["condition"] == "service_healthy"
    healthcheck = " ".join(pg_backup["healthcheck"]["test"])
    assert "[c]rond" in healthcheck
    assert "test -w /backups" in healthcheck
    assert "test -w /var/lib/node-exporter/textfile_collector" in healthcheck
    assert "${BACKUP_HOST_DIR:-../../data/postgres-backups}:/backups" in pg_backup["volumes"]
    assert "pg_backup_data" not in _compose()["volumes"]

    assert env["POSTGRES_HOST"] == "postgres"
    assert env["POSTGRES_DB"] == "ratatoskr"
    assert env["POSTGRES_USER"] == "ratatoskr_app"
    assert env["BACKUP_CRON"] == "${BACKUP_CRON:-0 3 * * *}"
    assert env["BACKUP_RETENTION_DAYS"] == "${BACKUP_RETENTION_DAYS:-14}"
    assert env["APP_ENV"] == "production"
    assert env["BACKUP_ENCRYPTION_KEY"] == "${BACKUP_ENCRYPTION_KEY:-}"
    assert env["BACKUP_REQUIRE_ENCRYPTION"] == "true"
    assert env["BACKUP_S3_BUCKET"] == "${BACKUP_S3_BUCKET:-}"
    assert env["BACKUP_S3_ENDPOINT_URL"] == "${BACKUP_S3_ENDPOINT_URL:-}"


def test_postgres_plaintext_backup_override_is_scoped_to_dev_overlay() -> None:
    env = _env_map(_dev_compose()["services"]["pg-backup"])

    assert env["APP_ENV"] == "development"
    assert env["BACKUP_REQUIRE_ENCRYPTION"] == "${BACKUP_REQUIRE_ENCRYPTION:-true}"


def test_pg_backup_image_matches_production_postgres_major() -> None:
    # The pg-backup sidecar's pg_dump must match the server major: pg_dump
    # refuses to dump a server newer than itself, so a lagging backup image
    # silently breaks backups after a Postgres upgrade (prod moved to 17 while
    # the sidecar stayed on 16).
    dockerfile = (ROOT / "ops/docker/pg-backup/Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^FROM\s+postgres:(\S+)", dockerfile, re.MULTILINE)
    assert match is not None, "pg-backup Dockerfile must build FROM a postgres image"

    backup_major = match.group(1).split("-", 1)[0]
    prod_major = _postgres_major(_compose()["services"]["postgres"]["image"])
    assert backup_major == prod_major, (
        f"pg-backup builds FROM postgres:{match.group(1)} but production compose is "
        f"Postgres {prod_major}; pg_dump must match the server major"
    )


def test_postgres_backup_metrics_are_scraped_by_node_exporter() -> None:
    services = _compose()["services"]
    node_exporter = services["node-exporter"]
    pg_backup = services["pg-backup"]

    assert "--collector.textfile.directory=/textfile" in node_exporter["command"]
    assert "pg_backup_metrics:/textfile:ro" in node_exporter["volumes"]
    assert "pg_backup_metrics:/var/lib/node-exporter/textfile_collector" in pg_backup["volumes"]
    assert "pg_backup_metrics" in _compose()["volumes"]


def test_postgres_backup_script_creates_metadata_and_optional_remote_copy() -> None:
    script = (ROOT / "ops/docker/pg-backup/run-backup.sh").read_text(encoding="utf-8")

    assert "pg_dump \\" in script
    assert "--format=custom" in script
    assert "openssl enc -aes-256-cbc -pbkdf2 -salt" in script
    assert "sha256sum" in script
    assert '"timestamp":' in script
    assert '"size_bytes":' in script
    assert '"sha256":' in script
    assert "BACKUP_S3_BUCKET" in script
    assert "aws $endpoint_args s3 cp" in script
    assert "ratatoskr_pg_backup_last_success_timestamp_seconds" in script
    assert "umask 077" in script
    assert "${BACKUP_REQUIRE_ENCRYPTION:-true}" in script
    assert "${APP_ENV:-production}" in script
    assert "allowed only when APP_ENV=development or APP_ENV=test" in script
    assert "BACKUP_ENCRYPTION_KEY is required when BACKUP_S3_BUCKET is set" in script


def test_postgres_backup_alert_fires_when_stale_or_absent() -> None:
    rules = yaml.safe_load((ROOT / "ops/monitoring/alerting_rules.yml").read_text(encoding="utf-8"))
    alerts = [
        rule
        for group in rules["groups"]
        for rule in group["rules"]
        if rule.get("alert") == "RatatoskrPostgresBackupStale"
    ]

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["labels"]["severity"] == "critical"
    assert "ratatoskr_pg_backup_last_success_timestamp_seconds" in alert["expr"]
    assert "> 129600" in alert["expr"]
    assert "absent(ratatoskr_pg_backup_last_success_timestamp_seconds)" in alert["expr"]


def test_disaster_recovery_runbook_covers_restore_drill_contract() -> None:
    runbook = (ROOT / "docs/runbooks/disaster-recovery.md").read_text(encoding="utf-8")

    for expected in (
        "RTO",
        "RPO",
        "PostgreSQL Restore",
        "Qdrant Restore Or Rebuild",
        "Redis Restore Or Reset",
        "Verification Checklist",
        "Communication Templates",
        "Backup Encryption Key Rotation During Restore",
        "Quarterly Drill",
        "Drill Sign-Off",
    ):
        assert expected in runbook

    assert ".github/ISSUE_TEMPLATE/disaster-recovery-drill.md" in runbook
    assert "tools/scripts/restore_smoke.sh tests/fixtures/restore_smoke.dump" in runbook


def test_disaster_recovery_drill_template_collects_required_evidence() -> None:
    template = (ROOT / ".github/ISSUE_TEMPLATE/disaster-recovery-drill.md").read_text(
        encoding="utf-8"
    )

    for expected in (
        "Metadata SHA256",
        "Measured RTO",
        "Measured RPO",
        "Postgres row counts",
        "Latest summary timestamp",
        "Qdrant collection counts",
        "Redis restore/reset result",
        "Append the completed drill to the runbook sign-off table",
    ):
        assert expected in template


def _postgres_major(image: str) -> str:
    """Extract the Postgres major version from a `postgres:<tag>` image ref.

    Handles both plain (`postgres:17`) and variant (`postgres:17-alpine`) tags.
    """
    tag = image.split(":", 1)[1]
    return tag.split("-", 1)[0]


def test_postgres_ci_jobs_match_production_major_version() -> None:
    # The migration/restore smoke tests and the Postgres-gated suite must run
    # against the same Postgres major the production compose stack runs, or CI
    # silently misses version-specific migration/restore breakage (prod moved to
    # Postgres 17 while these jobs lagged on 16).
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    prod_major = _postgres_major(_compose()["services"]["postgres"]["image"])

    for job_name in ("migration-smoke-test", "restore-smoke-test", "postgres-tests"):
        image = workflow["jobs"][job_name]["services"]["postgres"]["image"]
        assert _postgres_major(image) == prod_major, (
            f"{job_name} runs {image!r} but production compose is Postgres {prod_major}"
        )


def test_restore_smoke_ci_job_loads_dump_and_gates_status() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    restore_job = jobs["restore-smoke-test"]
    status_job = jobs["status-check"]

    assert _postgres_major(restore_job["services"]["postgres"]["image"]) == _postgres_major(
        _compose()["services"]["postgres"]["image"]
    )
    assert restore_job["needs"] == "prepare-environment"
    restore_steps = "\n".join(str(step) for step in restore_job["steps"])
    assert "app/db/" in restore_steps
    assert "postgresql-client" in restore_steps
    assert "tools/scripts/restore_smoke.sh tests/fixtures/restore_smoke.dump" in restore_steps
    assert "restore-smoke-test" in status_job["needs"]
    status_steps = "\n".join(str(step) for step in status_job["steps"])
    assert "needs.restore-smoke-test.result" in status_steps


def test_migration_smoke_ci_job_runs_full_roundtrip_not_single_step() -> None:
    # The migration smoke test must exercise EVERY downgrade() (head -> base ->
    # head) via the round-trip script, not the shallow `alembic downgrade -1` on
    # an empty DB that only touched the single latest migration.
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    migration_job = jobs["migration-smoke-test"]
    status_job = jobs["status-check"]

    steps = "\n".join(str(step) for step in migration_job["steps"])
    assert "tools/scripts/migration_roundtrip.sh" in steps
    assert "downgrade -1" not in steps, "single-step downgrade must not return"
    assert "migration-smoke-test" in status_job["needs"]
    status_steps = "\n".join(str(step) for step in status_job["steps"])
    assert "needs.migration-smoke-test.result" in status_steps


def test_migration_roundtrip_script_exercises_full_downgrade_with_data() -> None:
    script = (ROOT / "tools/scripts/migration_roundtrip.sh").read_text(encoding="utf-8")
    seed = (ROOT / "tools/scripts/seed_migration_roundtrip.py").read_text(encoding="utf-8")

    # Full round-trip: apply, seed, downgrade all the way to base, re-upgrade.
    assert "app.cli.migrate_db --apply" in script
    assert "tools.scripts.seed_migration_roundtrip" in script
    assert "alembic downgrade base" in script
    assert "alembic upgrade head" in script
    assert "downgrade -1" not in script

    # The seed must cover the documented 0006 data-dependent hotspot: two users
    # sharing a duplicate github_id across two repositories rows.
    assert "Repository(" in seed
    assert seed.count("_DUPLICATE_GITHUB_ID") >= 3  # constant def + both repo rows


def test_restore_smoke_script_uses_real_pg_restore_archive() -> None:
    script = (ROOT / "tools/scripts/restore_smoke.sh").read_text(encoding="utf-8")
    fixture = ROOT / "tests/fixtures/restore_smoke.dump"

    assert fixture.read_bytes().startswith(b"PGDMP")
    assert "pg_restore" in script
    assert "python -m app.cli.migrate_db" in script
    assert "restore_smoke_seed" in script
    assert "alembic_version" in script


def test_release_workflow_publishes_stable_but_not_latest() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    tags = workflow["jobs"]["push-docker-tag"]["steps"][4]["with"]["tags"]

    assert "type=raw,value=stable" in tags
    assert "latest" not in tags


def test_compose_app_services_check_schema_without_auto_migrate_dependency() -> None:
    services = _compose()["services"]

    for name in ("ratatoskr", "worker", "mobile-api"):
        service = services[name]
        command = "\n".join(str(part) for part in service["command"])
        assert "python -m app.cli.migrate_db --check" in command
        assert "migrate" not in service.get("depends_on", {})

    assert "migrate" not in services["scheduler"].get("depends_on", {})


def test_pi_deploy_keeps_previous_image_and_does_not_apply_migrations_on_restart() -> None:
    script = _pi_deploy_script()
    restart_branch = script.split("if [[ $RESTART -eq 1 ]]; then", maxsplit=1)[1]

    restart_call = "up -d --no-build --no-deps --force-recreate ${svc}"
    assert restart_call in script
    assert "tag_running_image_as_previous" in restart_branch
    assert restart_branch.index("tag_running_image_as_previous") < restart_branch.index(
        restart_call
    )
    assert "run_remote_migrations" not in restart_branch


def test_pi_deploy_ships_and_starts_postgres_backup_without_remote_build() -> None:
    script = _pi_deploy_script()
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    pi_overlay = (ROOT / "ops/docker/docker-compose.pi.yml").read_text(encoding="utf-8")

    assert "BACKUP_DOCKERFILE=ops/docker/pg-backup/Dockerfile" in script
    assert "BACKUP_SERVICES=(pg-backup)" in script
    assert 'build_and_ship "$BACKUP_DOCKERFILE" -- "${BACKUP_TO_BUILD[@]}"' in script
    assert "up -d --no-build --no-deps --force-recreate ${svc}" in script
    assert "--entrypoint sh -v ${COMPOSE_PROJECT}_pg_backup_metrics:/textfile" in script
    assert '--services "ratatoskr worker scheduler mobile-api pg-backup"' in makefile
    assert "BACKUP_RUN_ON_START=${BACKUP_RUN_ON_START:-true}" in pi_overlay


def test_pi_deploy_preserves_service_dns_alias_when_restoring_default_network() -> None:
    script = _pi_deploy_script()

    assert "docker network connect --alias '${svc}' docker_default" in script


def test_local_docker_deploy_builds_the_compose_image_it_starts() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("docker-deploy:", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]

    compose_build = "docker compose -f $(COMPOSE_FILE) build ratatoskr"
    compose_up = "docker compose -f $(COMPOSE_FILE) up -d --no-deps --force-recreate ratatoskr"
    assert compose_build in target
    assert compose_up in target
    assert "docker compose -f $(COMPOSE_FILE) down" not in target
    assert target.index(compose_build) < target.index(compose_up)
    assert "docker-build" not in target


def test_pi_deploy_waits_for_health_after_image_verification() -> None:
    script = _pi_deploy_script()
    restart_branch = script.split("elif [[ $RESTART -eq 1 ]]; then", maxsplit=1)[1]

    assert "PI_HEALTH_TIMEOUT_SECONDS=${PI_HEALTH_TIMEOUT_SECONDS:-240}" in script
    assert "PI_HEALTH_POLL_SECONDS=${PI_HEALTH_POLL_SECONDS:-5}" in script
    assert "local deadline=$((SECONDS + PI_HEALTH_TIMEOUT_SECONDS))" in script
    assert 'wait_for_service_health "$svc"' in restart_branch
    assert restart_branch.index('restart_service_verified "$svc"') < restart_branch.index(
        'wait_for_service_health "$svc"'
    )
    assert "{{.State.Health.Status}}" in script
    assert "timed out after ${PI_HEALTH_TIMEOUT_SECONDS}s" in script
    assert "docker inspect --format '{{json .State}}'" in script
    assert "logs --no-color --tail=50 ${svc}" in script


def test_pi_deploy_has_explicit_migrate_apply_and_rollback_paths() -> None:
    script = _pi_deploy_script()
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "MIGRATE_SERVICE=migrate" in script
    assert "--migrate-only" in script
    assert "--apply" in script
    assert "run_remote_migrations" in script
    migrate_command = "run --rm ${MIGRATE_SERVICE} python -m app.cli.migrate_db ${migrate_args[*]}"
    assert migrate_command in script
    assert "run --rm --no-build ${MIGRATE_SERVICE}" not in script
    assert "--rollback" in script
    assert "rollback_service_image" in script
    assert "docker tag \\\"\\$PREVIOUS_ID\\\" '${latest_tag}'" in script
    assert "pi-migrate:" in makefile
    assert "APPLY" in makefile
    assert "pi-rollback:" in makefile


def test_pi_deploy_emits_deploy_version_textfile_metric() -> None:
    script = _pi_deploy_script()

    assert "org.opencontainers.image.revision" in script
    assert "org.opencontainers.image.created" in script
    assert "ratatoskr_deploy_version_info" in script
    assert "${COMPOSE_PROJECT}_pg_backup_metrics:/textfile" in script
