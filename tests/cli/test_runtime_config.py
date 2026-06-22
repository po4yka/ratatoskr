"""Regression tests for app.cli._runtime.prepare_config."""

from __future__ import annotations

from argparse import Namespace
from dataclasses import dataclass

import pytest

from app.cli import _runtime
from app.config.runtime import RuntimeConfig


@dataclass(frozen=True)
class _StubCfg:
    runtime: RuntimeConfig


def test_log_level_override_applies_without_typeerror(monkeypatch: pytest.MonkeyPatch) -> None:
    """--log-level must override RuntimeConfig.

    RuntimeConfig is a (frozen) pydantic BaseModel, not a dataclass. The old code
    called dataclasses.replace() on it, which raised
    "TypeError: replace() should be called on dataclass instances" whenever
    --log-level was passed. Guard the model_copy() fix.
    """
    monkeypatch.setattr(_runtime, "load_config", lambda **_: _StubCfg(runtime=RuntimeConfig()))
    args = Namespace(log_level="DEBUG", env_file=None, db_path=None)

    cfg = _runtime.prepare_config(args)

    assert cfg.runtime.log_level == "DEBUG"


def test_no_log_level_keeps_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_runtime, "load_config", lambda **_: _StubCfg(runtime=RuntimeConfig()))
    args = Namespace(log_level=None, env_file=None, db_path=None)

    cfg = _runtime.prepare_config(args)

    assert cfg.runtime.log_level == "INFO"
