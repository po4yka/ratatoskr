"""Flag-gated summarize-graph runner (ADR-0013 / ADR-0018)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import app.application.graphs.summarize.runner as runner_mod
from app.application.graphs.summarize.runner import (
    is_summarize_graph_enabled,
    maybe_run_summarize_graph,
)


def _cfg(enabled: bool):
    return SimpleNamespace(runtime=SimpleNamespace(summarize_graph_enabled=enabled))


def test_is_enabled_reads_runtime_flag() -> None:
    assert is_summarize_graph_enabled(_cfg(True)) is True
    assert is_summarize_graph_enabled(_cfg(False)) is False


async def test_maybe_run_returns_none_when_flag_off(monkeypatch) -> None:
    run = AsyncMock()
    monkeypatch.setattr(runner_mod, "run_summarize_graph", run)

    out = await maybe_run_summarize_graph(
        cfg=_cfg(False),
        graph=MagicMock(),
        deps=MagicMock(),
        correlation_id="c",
        request_id=1,
        lang="en",
    )

    assert out is None  # caller falls back to the legacy path
    run.assert_not_awaited()


async def test_maybe_run_delegates_when_flag_on(monkeypatch) -> None:
    sentinel = {"ok": True}
    run = AsyncMock(return_value=sentinel)
    monkeypatch.setattr(runner_mod, "run_summarize_graph", run)

    out = await maybe_run_summarize_graph(
        cfg=_cfg(True),
        graph=MagicMock(),
        deps=MagicMock(),
        correlation_id="c",
        request_id=1,
        lang="en",
    )

    assert out is sentinel
    run.assert_awaited_once()


def test_real_config_defaults_flag_off() -> None:
    from tests.conftest import make_test_app_config

    cfg = make_test_app_config()
    assert cfg.runtime.summarize_graph_enabled is False
