from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
PI_DEPLOY_SCRIPT = ROOT / "tools/scripts/build-and-deploy-pi.sh"


def _compose() -> dict[str, Any]:
    return yaml.safe_load((ROOT / "ops/docker/docker-compose.yml").read_text(encoding="utf-8"))


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


def test_default_compose_stack_contains_core_services_without_profiles() -> None:
    services = _compose()["services"]

    for name in ("ratatoskr", "mobile-api", "redis", "qdrant"):
        assert name in services
        assert "profiles" not in services[name]

    assert services["mobile-api"]["ports"] == ["127.0.0.1:18000:8000"]


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

    for name in ("prometheus", "grafana", "loki", "promtail", "node-exporter"):
        assert name in services
        assert services[name]["profiles"] == ["with-monitoring"]


def test_release_workflow_publishes_stable_but_not_latest() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    tags = workflow["jobs"]["push-docker-tag"]["steps"][4]["with"]["tags"]

    assert "type=raw,value=stable" in tags
    assert "latest" not in tags


def test_pi_deploy_runs_migrations_before_recreating_services() -> None:
    script = _pi_deploy_script()
    restart_branch = script.split("if [[ $RESTART -eq 1 ]]; then", maxsplit=1)[1]

    restart_call = "up -d --no-deps --force-recreate ${svc}"
    assert restart_call in script
    assert restart_branch.index("run_remote_migrations") < restart_branch.index(
        'for svc in "${SERVICES[@]}"; do'
    )
    assert "run --rm --no-build ${MIGRATE_SERVICE}" in script
    assert "up -d --no-build postgres" in script


def test_pi_deploy_ships_migrate_image_for_restart_flows() -> None:
    script = _pi_deploy_script()

    assert "MIGRATE_SERVICE=migrate" in script
    assert 'SHARED_TO_BUILD+=("$MIGRATE_SERVICE")' in script
    assert "--skip-migrate" in script
    assert "WARNING: skipping database migrations" in script
