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
