from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


WORKFLOW_PATH = Path(__file__).parents[2] / ".github" / "workflows" / "ci.yml"
NON_GATING_JOBS = {"pr-summary", "status-check"}


def _jobs() -> dict[str, dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text())
    return workflow["jobs"]


def test_status_check_depends_on_every_gating_job() -> None:
    jobs = _jobs()

    assert set(jobs["status-check"]["needs"]) == set(jobs) - NON_GATING_JOBS


def test_pr_summary_waits_for_every_gating_job() -> None:
    jobs = _jobs()

    assert set(jobs["pr-summary"]["needs"]) == set(jobs) - NON_GATING_JOBS


def test_status_check_evaluates_the_complete_needs_context() -> None:
    step = _jobs()["status-check"]["steps"][0]

    assert step["env"]["NEEDS_JSON"] == "${{ toJSON(needs) }}"
    assert 'json.loads(os.environ["NEEDS_JSON"])' in step["run"]
