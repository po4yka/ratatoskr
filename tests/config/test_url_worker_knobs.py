"""Tests for the url_worker_enqueue_enabled and url_worker_concurrency knobs.

Covers:
- Default values for both fields
- Environment-variable overrides
- Validation: concurrency out-of-range values are rejected
- The rollback knob: url_worker_enqueue_enabled can be set to false
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.config.runtime import RuntimeConfig


class TestUrlWorkerEnqueueEnabled:
    def test_default_is_true(self) -> None:
        cfg = RuntimeConfig.model_validate({})
        assert cfg.url_worker_enqueue_enabled is True

    def test_env_override_disables(self) -> None:
        cfg = RuntimeConfig.model_validate({"URL_WORKER_ENQUEUE_ENABLED": "false"})
        assert cfg.url_worker_enqueue_enabled is False

    def test_env_override_enables(self) -> None:
        cfg = RuntimeConfig.model_validate({"URL_WORKER_ENQUEUE_ENABLED": "true"})
        assert cfg.url_worker_enqueue_enabled is True

    def test_zero_string_disables(self) -> None:
        cfg = RuntimeConfig.model_validate({"URL_WORKER_ENQUEUE_ENABLED": "0"})
        assert cfg.url_worker_enqueue_enabled is False

    def test_one_string_enables(self) -> None:
        cfg = RuntimeConfig.model_validate({"URL_WORKER_ENQUEUE_ENABLED": "1"})
        assert cfg.url_worker_enqueue_enabled is True

    def test_committed_config_enables_worker_handoff(self) -> None:
        config_path = Path(__file__).resolve().parents[2] / "config/ratatoskr.yaml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        assert config["runtime"]["url_worker_enqueue_enabled"] is True

    def test_url_processing_task_module_imports_in_worker_environment(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", "import app.tasks.url_processing"],
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr


class TestUrlWorkerConcurrency:
    def test_default_is_four(self) -> None:
        cfg = RuntimeConfig.model_validate({})
        assert cfg.url_worker_concurrency == 4

    def test_env_override_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"URL_WORKER_CONCURRENCY": "8"})
        assert cfg.url_worker_concurrency == 8

    def test_taskiq_per_process_name_takes_precedence_over_legacy_alias(self) -> None:
        cfg = RuntimeConfig.model_validate(
            {
                "TASKIQ_MAX_ASYNC_TASKS_PER_PROCESS": "3",
                "URL_WORKER_CONCURRENCY": "8",
            }
        )
        assert cfg.url_worker_concurrency == 3

    def test_minimum_boundary_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"URL_WORKER_CONCURRENCY": "1"})
        assert cfg.url_worker_concurrency == 1

    def test_maximum_boundary_accepted(self) -> None:
        cfg = RuntimeConfig.model_validate({"URL_WORKER_CONCURRENCY": "16"})
        assert cfg.url_worker_concurrency == 16

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"URL_WORKER_CONCURRENCY": "0"})

    def test_above_max_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RuntimeConfig.model_validate({"URL_WORKER_CONCURRENCY": "17"})
