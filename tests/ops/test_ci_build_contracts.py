from __future__ import annotations

import os
import re
import tarfile
import tomllib
from pathlib import Path
from typing import Any

import yaml

from tools.scripts.build_web_bundle import _write_bundle

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
            "ops/docker/ratatoskr-web.commit",
            "ops/docker/ratatoskr-web.bundle.tar.gz",
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
            "ops/docker/ratatoskr-web.commit",
            "ops/docker/ratatoskr-web.bundle.tar.gz",
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


def test_release_images_build_the_pinned_frontend_and_ignore_local_assets() -> None:
    revision = (ROOT / "ops/docker/ratatoskr-web.commit").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"[0-9a-f]{40}", revision)

    bundle_path = ROOT / "ops/docker/ratatoskr-web.bundle.tar.gz"
    assert bundle_path.stat().st_size > 0
    with tarfile.open(bundle_path, mode="r:gz") as bundle:
        names = bundle.getnames()
        assert "index.html" in names
        assert any(name.startswith("assets/") and name.endswith(".js") for name in names)
        assert all(not name.startswith("/") and ".." not in Path(name).parts for name in names)
        source_commit = bundle.extractfile(".source-commit")
        assert source_commit is not None
        assert source_commit.read().decode().strip() == revision

    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "app/static/web/" in dockerignore

    for dockerfile_name in ("Dockerfile", "Dockerfile.api"):
        dockerfile = (ROOT / "ops/docker" / dockerfile_name).read_text(encoding="utf-8")
        assert "ADD ops/docker/ratatoskr-web.bundle.tar.gz ./app/static/web/" in dockerfile
        assert "test -s /app/app/static/web/index.html" in dockerfile
        assert "test -s /app/app/static/web/.source-commit" in dockerfile

    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    pi_deploy_all = next(
        line for line in makefile.splitlines() if line.startswith("pi-deploy-all:")
    )
    assert pi_deploy_all == "pi-deploy-all:"


def test_frontend_bundle_generation_is_deterministic(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text("<script src='/assets/app.js'></script>", encoding="utf-8")
    (assets / "app.js").write_text("console.log('ratatoskr')", encoding="utf-8")

    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    revision = "1" * 40

    first_digest = _write_bundle(dist, first, revision)
    os.utime(dist / "index.html", (1_900_000_000, 1_900_000_000))
    second_digest = _write_bundle(dist, second, revision)

    assert first_digest == second_digest
    assert first.read_bytes() == second.read_bytes()


def test_postgres_tests_have_one_marker_driven_ci_job() -> None:
    pytest_config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "pytest"
    ]["ini_options"]
    assert any(marker.startswith("postgres:") for marker in pytest_config["markers"])

    jobs = _workflow("ci.yml")["jobs"]
    unit_command = _step_named(jobs["test"], "Run unit tests with coverage")["run"]
    integration_command = _step_named(jobs["integration-tests"], "Run integration tests")["run"]
    postgres_command = _step_named(jobs["postgres-tests"], "Run all PostgreSQL tests")["run"]

    assert '-m "not integration and not postgres"' in unit_command
    assert '-m "integration and not postgres"' in integration_command
    assert "tests/" in postgres_command
    assert '-m "postgres"' in postgres_command
    assert "tests/parity" not in postgres_command

    conftest = (ROOT / "tests/conftest.py").read_text(encoding="utf-8")
    assert "@pytest.hookimpl(tryfirst=True)" in conftest
    assert '_POSTGRES_FIXTURE_NAMES = frozenset({"database", "db", "session"})' in conftest
