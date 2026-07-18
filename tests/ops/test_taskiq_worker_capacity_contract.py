from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _compose(path: str) -> dict[str, Any]:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def _worker_script() -> str:
    command = _compose("ops/docker/docker-compose.yml")["services"]["worker"]["command"]
    assert command[:2] == ["sh", "-c"]
    return command[2]


def test_taskiq_worker_bounds_execution_and_prefetch_per_process() -> None:
    script = _worker_script()

    assert "exec python -m app.cli.taskiq_worker" in script
    assert "app.tasks.url_processing" in script
    assert "exec taskiq worker" not in script


def test_pi_overlay_uses_explicit_per_process_taskiq_knobs() -> None:
    overlay = (ROOT / "ops/docker/docker-compose.pi.yml").read_text(encoding="utf-8")

    assert "TASKIQ_WORKER_PROCESSES=${TASKIQ_WORKER_PROCESSES:-1}" in overlay
    assert "TASKIQ_MAX_ASYNC_TASKS_PER_PROCESS=${TASKIQ_MAX_ASYNC_TASKS_PER_PROCESS:-2}" in overlay
    assert "TASKIQ_WORKER_CONCURRENCY=" not in overlay
