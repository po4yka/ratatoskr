from __future__ import annotations

from pathlib import Path

import yaml

from app.config.integrations import VectorReconcileConfig


def test_committed_vector_reconcile_config_matches_convergence_defaults() -> None:
    config_path = Path(__file__).resolve().parents[2] / "config/ratatoskr.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    defaults = VectorReconcileConfig()

    assert config["vector_reconcile"] == {
        "cron": defaults.cron,
        "batch_size": defaults.batch_size,
    }
