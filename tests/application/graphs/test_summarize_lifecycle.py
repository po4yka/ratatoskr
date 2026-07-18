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
    assert kwargs["raise_on_error"] is True


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


# ── persist-everything on terminal failure: accumulated + attached llm_calls ──


def _deps_with_llm_repo() -> MagicMock:
    deps = MagicMock()
    deps.llm_repo.async_insert_llm_call = AsyncMock()
    return deps


async def test_route_terminal_failure_persists_recovered_llm_calls(monkeypatch) -> None:
    """The whole repair loop's accumulated checkpoint llm_calls (summarize + each
    repair attempt) must be written on a terminal failure -- before this fix the
    terminal path used the empty initial_state and dropped them all (rule 3)."""
    monkeypatch.setattr(lifecycle_mod, "persist_request_failure", AsyncMock())
    deps = _deps_with_llm_repo()
    recovered = [
        {"request_id": 7, "status": "ok", "model": "m", "attempt_trigger": "graph_node"},
        {"request_id": 7, "status": "error", "model": "m", "attempt_trigger": "graph_node"},
    ]

    msg = await route_terminal_failure(
        _state(), deps, CallBudgetExceeded("exhausted"), recovered_llm_calls=recovered
    )

    assert "Error ID: corr-9" in msg
    persisted = [c.args[0] for c in deps.llm_repo.async_insert_llm_call.await_args_list]
    assert persisted == recovered


async def test_route_terminal_failure_orders_recovered_before_attached(monkeypatch) -> None:
    """Recovered checkpoint rows (summarize + repairs) insert BEFORE the
    exception-attached failure row, so the repository-assigned attempt_index stays
    chronological (the accumulated calls really did happen first)."""
    monkeypatch.setattr(lifecycle_mod, "persist_request_failure", AsyncMock())
    deps = _deps_with_llm_repo()
    recovered = [{"request_id": 7, "status": "ok", "tag": "accumulated"}]
    err = RuntimeError("llm boom")
    # A summarize-node raise attaches its failure row here (GAP 3a); disjoint from
    # the checkpoint rows (a node commits XOR attaches, never both).
    err.llm_failure_records = [{"request_id": 7, "status": "error", "tag": "attached"}]  # type: ignore[attr-defined]

    await route_terminal_failure(_state(), deps, err, recovered_llm_calls=recovered)

    order = [c.args[0]["tag"] for c in deps.llm_repo.async_insert_llm_call.await_args_list]
    assert order == ["accumulated", "attached"]


async def test_route_terminal_failure_skips_recovered_without_request_id(monkeypatch) -> None:
    """Content-only path (request_id None): recovered rows would FK-violate
    ``requests.id``, so they are skipped."""
    monkeypatch.setattr(lifecycle_mod, "persist_request_failure", AsyncMock())
    deps = _deps_with_llm_repo()

    await route_terminal_failure(
        _state(request_id=None),
        deps,
        RuntimeError("x"),
        recovered_llm_calls=[{"request_id": None, "status": "ok"}],
    )

    deps.llm_repo.async_insert_llm_call.assert_not_awaited()


async def test_route_terminal_failure_recovered_persist_error_does_not_block(monkeypatch) -> None:
    """A failing recovered-row insert is logged and skipped; the ERROR finalization
    (persist_request_failure) still runs -- one bad row never blocks completion."""
    persist = AsyncMock()
    monkeypatch.setattr(lifecycle_mod, "persist_request_failure", persist)
    deps = _deps_with_llm_repo()
    deps.llm_repo.async_insert_llm_call = AsyncMock(side_effect=RuntimeError("db down"))

    msg = await route_terminal_failure(
        _state(),
        deps,
        CallBudgetExceeded("x"),
        recovered_llm_calls=[{"request_id": 7, "status": "ok"}],
    )

    assert "Error ID: corr-9" in msg
    persist.assert_awaited_once()
