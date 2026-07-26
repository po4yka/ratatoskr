from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = ROOT / ".github/workflows/benchmarks.yml"
CI_WORKFLOW_PATH = ROOT / ".github/workflows/ci.yml"


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))


def _ci_workflow() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW_PATH.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    # PyYAML follows YAML 1.1 and parses the unquoted GitHub Actions `on` key as True.
    return workflow.get("on", workflow.get(True, {}))


def test_full_benchmarks_run_only_on_schedule_or_manual_dispatch() -> None:
    workflow = _workflow()
    triggers = _triggers(workflow)

    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert triggers["schedule"] == [{"cron": "0 4 * * 1"}]
    assert workflow["permissions"] == {"contents": "read"}


def test_full_benchmarks_use_the_locked_test_group_and_write_json_baseline() -> None:
    job = _workflow()["jobs"]["full-benchmarks"]
    steps = job["steps"]
    sync_step = next(step for step in steps if step["name"] == "Install benchmark dependencies")
    benchmark_step = next(step for step in steps if step["name"] == "Run full benchmark suite")

    assert sync_step["run"] == "uv sync --frozen --no-default-groups --group ci-test"

    command = benchmark_step["run"]
    assert "uv run --no-sync pytest" in command
    assert "tests/benchmarks/" in command
    assert "--benchmark-only" in command
    assert "--benchmark-json=benchmark-baseline.json" in command
    assert "-n " not in command


def test_benchmark_baseline_is_uploaded_with_pinned_actions() -> None:
    steps = _workflow()["jobs"]["full-benchmarks"]["steps"]
    uses = [str(step["uses"]) for step in steps if "uses" in step]

    assert uses
    assert all(re.fullmatch(r"[^@]+@[0-9a-f]{40}", action) for action in uses)

    upload = next(step for step in steps if step["name"] == "Upload benchmark baseline")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "benchmark-baseline.json"
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] == 90


def test_pr_ci_runs_only_the_short_regression_budget() -> None:
    steps = _ci_workflow()["jobs"]["test"]["steps"]
    budget_step = next(
        step for step in steps if step["name"] == "Run PR benchmark regression budget"
    )

    assert budget_step["if"] == "github.event_name == 'pull_request'"
    assert budget_step["env"]["JSON_VALIDATION_P99_TARGET_MS"] == "10"

    command = budget_step["run"]
    expected_cases = (
        "test_validate_and_shape_throughput",
        "test_aggregation_sentence_cache_scan",
        "test_vector_point_id_generation_batch",
    )
    assert all(case in command for case in expected_cases)
    assert "tests/benchmarks/ \\" not in command
    assert "--benchmark-max-time=0.05" in command
    assert "--benchmark-min-rounds=1" in command
    assert "--benchmark-warmup=off" in command

    unit_command = next(
        step["run"] for step in steps if step["name"] == "Run unit tests with coverage"
    )
    assert "--benchmark-only" not in unit_command
