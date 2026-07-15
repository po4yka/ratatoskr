from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8"))


def _docker_build_step(job: dict[str, Any]) -> dict[str, Any]:
    return next(
        step
        for step in job["steps"]
        if str(step.get("uses", "")).startswith("docker/build-push-action@")
    )


def _step_named(job: dict[str, Any], name: str) -> dict[str, Any]:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_buildkit_cache_exports_are_scoped_small_and_time_bounded() -> None:
    ci_jobs = _workflow("ci.yml")["jobs"]
    release_jobs = _workflow("release.yml")["jobs"]
    builds = {
        "bot-${{ runner.arch }}": _docker_build_step(ci_jobs["docker-build"]),
        "api-${{ runner.arch }}": _docker_build_step(ci_jobs["docker-api-browser-smoke"]),
        "release-multiarch": _docker_build_step(release_jobs["push-docker-tag"]),
    }

    for scope, step in builds.items():
        inputs = step["with"]
        assert inputs["cache-from"] == f"type=gha,scope={scope},timeout=5m"
        assert inputs["cache-to"] == (
            f"type=gha,scope={scope},mode=min,timeout=5m,ignore-error=true"
        )


def test_docker_builds_are_path_aware_and_browser_smoke_stays_in_buildkit() -> None:
    jobs = _workflow("ci.yml")["jobs"]
    expected_inputs = {
        "docker-build": {
            "ops/docker/Dockerfile",
            ".dockerignore",
            "app/**",
            "bot.py",
            "alembic.ini",
            "config/**",
            "pyproject.toml",
            "uv.lock",
        },
        "docker-api-browser-smoke": {
            "ops/docker/Dockerfile.api",
            ".dockerignore",
            "app/**",
            "alembic.ini",
            "config/**",
            "pyproject.toml",
            "uv.lock",
        },
    }

    for job_name, paths in expected_inputs.items():
        job = jobs[job_name]
        filter_config = yaml.safe_load(
            _step_named(job, "Detect Docker-relevant changes")["with"]["filters"]
        )
        assert set(filter_config["docker"]) == paths
        assert _step_named(job, "Set up Docker Buildx")["if"] == (
            "steps.filter.outputs.docker == 'true'"
        )
        assert _docker_build_step(job)["if"] == "steps.filter.outputs.docker == 'true'"

    api_job = jobs["docker-api-browser-smoke"]
    api_inputs = _docker_build_step(api_job)["with"]
    assert api_inputs["target"] == "browser-smoke"
    assert api_inputs["load"] is False
    assert "tags" not in api_inputs
    assert all("docker run" not in str(step.get("run", "")) for step in api_job["steps"])

    dockerfile = (ROOT / "ops/docker/Dockerfile.api").read_text(encoding="utf-8")
    assert "FROM runtime-base AS browser-smoke" in dockerfile
    assert dockerfile.rstrip().endswith("FROM runtime-base AS runtime")
