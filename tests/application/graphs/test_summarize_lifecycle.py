"""Single terminal-failure path (ADR-0011 / ADR-0018): no parallel error path."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import app.application.graphs.summarize.lifecycle as lifecycle_mod
from app.application.graphs.summarize.lifecycle import (
    CallBudgetExceeded,
    error_id_message,
    route_terminal_failure,
)
from app.application.graphs.summarize.state import SummarizeState


def _state(**over: object) -> SummarizeState:
    base: dict = {"correlation_id": "corr-9", "request_id": 7, "lang": "en"}
    base.update(over)
    return base  # type: ignore[return-value]


def test_error_id_message_uses_correlation_id() -> None:
    assert "Error ID: corr-9" in error_id_message("corr-9", 7)


def test_error_id_message_falls_back_to_request_id() -> None:
    assert "Error ID: 7" in error_id_message(None, 7)


def test_error_id_message_unknown_when_both_missing() -> None:
    assert "Error ID: unknown" in error_id_message(None, None)


def test_call_budget_exceeded_is_exception_subclass() -> None:
    assert issubclass(CallBudgetExceeded, Exception)


async def test_route_terminal_failure_persists_and_returns_error_id(monkeypatch) -> None:
    persist = AsyncMock()
    monkeypatch.setattr(lifecycle_mod, "persist_request_failure", persist)
    deps = MagicMock()

    msg = await route_terminal_failure(_state(), deps, RuntimeError("boom"))

    assert "Error ID: corr-9" in msg
    persist.assert_awaited_once()
    kwargs = persist.await_args.kwargs
    assert kwargs["request_id"] == 7
    assert kwargs["correlation_id"] == "corr-9"
    assert kwargs["request_repo"] is deps.requests
    assert kwargs["retryable"] is False


async def test_route_terminal_failure_without_request_id_skips_persist(monkeypatch, caplog) -> None:
    import logging

    persist = AsyncMock()
    monkeypatch.setattr(lifecycle_mod, "persist_request_failure", persist)

    with caplog.at_level(logging.ERROR):
        msg = await route_terminal_failure(_state(request_id=None), MagicMock(), RuntimeError("x"))

    persist.assert_not_awaited()
    assert "Error ID: corr-9" in msg
    # Traceability: with no request row to attach the failure to, the only trace is
    # the log line -- it must still carry the correlation id.
    assert "summarize_graph_failure_without_request_id" in caplog.text
    assert any(getattr(r, "correlation_id", None) == "corr-9" for r in caplog.records)
