"""Summarize graph: conditional edge, per-invocation thread_id, failure mapping.

The runner tests use a stub compiled graph (no langgraph). The real compile /
ainvoke tests use an in-memory checkpointer and skip where ``graph`` is absent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.application.graphs.summarize.graph as graph_mod
from app.application.graphs.summarize.graph import (
    DEFAULT_RECURSION_LIMIT,
    _route_after_validate,
    build_summarize_graph,
    run_summarize_graph,
)
from app.application.graphs.summarize.lifecycle import CallBudgetExceeded

# ── conditional edge ─────────────────────────────────────────────────────────


def test_route_after_validate_valid_goes_to_enrich() -> None:
    assert _route_after_validate({"validation_errors": []}) == "enrich"


def test_route_after_validate_invalid_goes_to_repair() -> None:
    assert _route_after_validate({"validation_errors": ["bad"]}) == "repair"


def test_router_outputs_are_valid_conditional_route_keys() -> None:
    # CI-safe drift guard: the router's possible return values must all be keys of
    # the conditional-edge map the builder wires (renaming one without the other
    # would only fail the langgraph-gated compile test, which skips in CI).
    from app.application.graphs.summarize.graph import VALIDATE_ROUTES

    outputs = {
        _route_after_validate({"validation_errors": []}),
        _route_after_validate({"validation_errors": ["x"]}),
    }
    assert outputs <= set(VALIDATE_ROUTES)


# ── runner: thread_id == correlation_id, recursion_limit per-invocation ───────


async def test_run_sets_thread_id_to_correlation_id_per_invocation() -> None:
    final_state = {"correlation_id": "corr-1", "request_id": 5}
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value=final_state)

    out = await run_summarize_graph(
        graph=graph, deps=MagicMock(), correlation_id="corr-1", request_id=5, lang="en"
    )

    assert out is final_state
    config = graph.ainvoke.await_args.kwargs["config"]
    assert config["configurable"]["thread_id"] == "corr-1"  # sacred
    assert config["recursion_limit"] == DEFAULT_RECURSION_LIMIT


# ── runner: every failure mode routes to the single terminal helper ───────────


@pytest.mark.parametrize(
    "exc",
    [RuntimeError("node boom"), CallBudgetExceeded("budget exhausted")],
    ids=["node_exception", "call_budget_exceeded"],
)
async def test_run_maps_failures_to_single_terminal_path(monkeypatch, exc) -> None:
    route = AsyncMock(return_value="Processing failed (Error ID: corr-2). Please try again.")
    monkeypatch.setattr(graph_mod, "route_terminal_failure", route)
    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=exc)

    out = await run_summarize_graph(
        graph=graph, deps=MagicMock(), correlation_id="corr-2", request_id=9, lang="en"
    )

    route.assert_awaited_once()
    assert route.await_args.args[2] is exc  # the exact error object is routed
    assert "Error ID: corr-2" in out["error"]


async def test_run_maps_graph_recursion_error(monkeypatch) -> None:
    errors = pytest.importorskip("langgraph.errors")
    route = AsyncMock(return_value="Processing failed (Error ID: corr-3). Please try again.")
    monkeypatch.setattr(graph_mod, "route_terminal_failure", route)
    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=errors.GraphRecursionError("recursion limit"))

    out = await run_summarize_graph(
        graph=graph, deps=MagicMock(), correlation_id="corr-3", request_id=1, lang="en"
    )

    route.assert_awaited_once()
    assert "Error ID: corr-3" in out["error"]


# ── real langgraph: compile + end-to-end happy path ───────────────────────────


def _real_deps():
    from app.application.graphs.summarize.deps import SummarizeDeps

    m = MagicMock()
    return SummarizeDeps(
        llm_client=m, retrieval=m, extraction=m, stream_sink=m, summaries=m, requests=m
    )


async def test_build_compiles_and_runs_happy_path_with_in_memory_saver() -> None:
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import InMemorySaver

    graph = build_summarize_graph(deps=_real_deps(), checkpointer=InMemorySaver())
    out = await run_summarize_graph(
        graph=graph, deps=_real_deps(), correlation_id="corr-real", request_id=11, lang="en"
    )

    assert "error" not in out  # happy path traverses ingest -> ... -> notify -> END
    assert out["correlation_id"] == "corr-real"
    assert out["request_id"] == 11
    assert out["lang"] == "en"  # state field survives the full traversal unchanged
    assert out["validation_errors"] == []


async def test_compiled_validate_repair_loop_terminates_via_budget(monkeypatch) -> None:
    """End-to-end: a never-valid summary drives validate<->repair until the repair
    budget trips, terminating the compiled loop through the single terminal path."""
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import InMemorySaver

    import app.application.graphs.summarize.lifecycle as lifecycle_mod

    async def always_invalid(state, *, deps):
        return {"validation_errors": ["forced"]}

    # build_summarize_graph reads module-level _NODES; swap validate before building.
    patched_nodes = dict(graph_mod._NODES)
    patched_nodes["validate"] = always_invalid
    monkeypatch.setattr(graph_mod, "_NODES", patched_nodes)
    # Avoid the real DB write in the terminal path (deps are MagicMocks).
    monkeypatch.setattr(lifecycle_mod, "persist_request_failure", AsyncMock())

    graph = build_summarize_graph(deps=_real_deps(), checkpointer=InMemorySaver())
    out = await run_summarize_graph(
        graph=graph, deps=_real_deps(), correlation_id="corr-loop", request_id=12, lang="en"
    )

    # Loop terminated (did not hang) via CallBudgetExceeded -> terminal path.
    assert "Error ID: corr-loop" in out["error"]


async def test_di_compiles_with_default_in_memory_checkpointer() -> None:
    pytest.importorskip("langgraph")
    from app.di.graphs import build_summarize_graph_app

    graph = build_summarize_graph_app(deps=_real_deps())
    assert graph is not None
