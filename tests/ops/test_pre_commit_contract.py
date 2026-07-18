from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _config() -> dict[str, Any]:
    return yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))


def _hooks_by_id(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {hook["id"]: hook for repository in config["repos"] for hook in repository["hooks"]}


def test_fast_hooks_and_full_semgrep_scans_use_separate_stages() -> None:
    config = _config()
    hooks = _hooks_by_id(config)

    assert config["default_install_hook_types"] == ["pre-commit", "pre-push"]
    assert config["default_stages"] == ["pre-commit"]

    for hook_id in ("semgrep-mutability", "semgrep-bare-except"):
        hook = hooks[hook_id]
        assert hook["stages"] == ["pre-push"]
        assert hook["pass_filenames"] is False
        assert hook["always_run"] is True
        assert hook["entry"].endswith(" app/ tests/")


def test_pre_commit_mypy_uses_a_persistent_incremental_cache() -> None:
    mypy_args = _hooks_by_id(_config())["mypy"]["args"]

    assert "--cache-dir=.mypy_cache/pre-commit" in mypy_args
    assert "--no-incremental" not in mypy_args
    assert "--cache-dir=/dev/null" not in mypy_args
