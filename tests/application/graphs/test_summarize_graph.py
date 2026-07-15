"""Summarize graph: conditional edge, per-invocation thread_id, failure mapping.

The runner tests use a stub compiled graph (no langgraph). The real compile /
ainvoke tests use an in-memory checkpointer and skip where ``graph`` is absent.
"""

from __future__ import annotations

import asyncio
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


async def test_run_deletes_checkpoint_after_success() -> None:
    checkpointer = MagicMock(adelete_thread=AsyncMock())
    graph = MagicMock(
        ainvoke=AsyncMock(return_value={"summary": {"tldr": "ok"}}),
        checkpointer=checkpointer,
    )

    await run_summarize_graph(
        graph=graph, deps=MagicMock(), correlation_id="corr-clean", request_id=5, lang="en"
    )

    checkpointer.adelete_thread.assert_awaited_once_with("corr-clean")


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


# ── recovery of accumulated llm_calls on terminal failure (rule 3) ────────────


async def test_recover_accumulated_llm_calls_reads_checkpoint_state() -> None:
    from app.application.graphs.summarize.graph import recover_accumulated_llm_calls

    records = [{"request_id": 5, "status": "ok"}, {"request_id": 5, "status": "error"}]
    snapshot = MagicMock()
    snapshot.values = {"llm_calls": records, "correlation_id": "c"}
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=snapshot)

    out = await recover_accumulated_llm_calls(graph, {"configurable": {"thread_id": "c"}})

    assert out == records
    assert out is not records  # a copy, so mutating state later cannot corrupt it


async def test_recover_accumulated_llm_calls_swallows_checkpoint_error() -> None:
    from app.application.graphs.summarize.graph import recover_accumulated_llm_calls

    graph = MagicMock()
    graph.aget_state = AsyncMock(side_effect=RuntimeError("no checkpoint"))

    # Best-effort: an unreadable checkpoint must not raise a second error path.
    assert await recover_accumulated_llm_calls(graph, {"configurable": {"thread_id": "c"}}) == []


async def test_recover_accumulated_llm_calls_empty_when_key_absent() -> None:
    from app.application.graphs.summarize.graph import recover_accumulated_llm_calls

    snapshot = MagicMock()
    snapshot.values = {"correlation_id": "c"}  # failed before any LLM call
    graph = MagicMock()
    graph.aget_state = AsyncMock(return_value=snapshot)

    assert await recover_accumulated_llm_calls(graph, {}) == []


async def test_run_recovers_and_forwards_accumulated_llm_calls_on_failure(monkeypatch) -> None:
    """On a terminal failure the runner recovers checkpoint llm_calls and hands them
    to the single terminal sink -- so no accumulated summarize/repair call is
    dropped (rule 3)."""
    recovered = [{"request_id": 9, "status": "ok"}]
    monkeypatch.setattr(
        graph_mod, "recover_accumulated_llm_calls", AsyncMock(return_value=recovered)
    )
    route = AsyncMock(return_value="Processing failed (Error ID: corr-x). Please try again.")
    monkeypatch.setattr(graph_mod, "route_terminal_failure", route)
    graph = MagicMock()
    graph.ainvoke = AsyncMock(side_effect=CallBudgetExceeded("exhausted"))

    await run_summarize_graph(
        graph=graph, deps=MagicMock(), correlation_id="corr-x", request_id=9, lang="en"
    )

    route.assert_awaited_once()
    assert route.await_args.kwargs["recovered_llm_calls"] is recovered


async def test_run_deletes_checkpoint_only_after_terminal_recovery(monkeypatch) -> None:
    events: list[str] = []
    checkpointer = MagicMock(
        adelete_thread=AsyncMock(side_effect=lambda _thread_id: events.append("cleanup"))
    )
    graph = MagicMock(
        ainvoke=AsyncMock(side_effect=RuntimeError("boom")),
        checkpointer=checkpointer,
    )

    async def recover(_graph, _config):
        events.append("recover")
        return [{"status": "ok"}]

    async def route(*_args, **_kwargs):
        events.append("persist-terminal")
        return "Error ID: corr-fail"

    monkeypatch.setattr(graph_mod, "recover_accumulated_llm_calls", recover)
    monkeypatch.setattr(graph_mod, "route_terminal_failure", route)

    await run_summarize_graph(
        graph=graph, deps=MagicMock(), correlation_id="corr-fail", request_id=9, lang="en"
    )

    assert events == ["recover", "persist-terminal", "cleanup"]


async def test_run_preserves_checkpoint_on_cancellation() -> None:
    checkpointer = MagicMock(adelete_thread=AsyncMock())
    graph = MagicMock(
        ainvoke=AsyncMock(side_effect=asyncio.CancelledError()),
        checkpointer=checkpointer,
    )

    with pytest.raises(asyncio.CancelledError):
        await run_summarize_graph(
            graph=graph, deps=MagicMock(), correlation_id="corr-cancel", request_id=9, lang="en"
        )

    checkpointer.adelete_thread.assert_not_awaited()


# ── real langgraph: compile + end-to-end happy path ───────────────────────────


def _real_deps():
    from app.application.graphs.summarize.deps import SummarizeDeps

    m = MagicMock()
    return SummarizeDeps(
        llm_client=m,
        retrieval=m,
        extraction=m,
        stream_sink=m,
        summaries=m,
        requests=m,
        summary_index=m,
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


async def test_terminal_failure_persists_accumulated_llm_calls_end_to_end(monkeypatch) -> None:
    """Regression (rule 3): a run that fails via the repair budget must STILL persist
    every summarize + repair llm_calls record accumulated in the checkpoint.

    Before the fix the terminal path passed the empty ``initial_state`` to
    ``route_terminal_failure``, so the entire repair loop's calls were silently
    dropped. This drives the REAL compiled graph (with an InMemorySaver, matching
    production's always-present checkpointer) through the budget loop and asserts
    the injected ``llm_repo`` received all of them."""
    pytest.importorskip("langgraph")
    from langgraph.checkpoint.memory import InMemorySaver

    import app.application.graphs.summarize.lifecycle as lifecycle_mod
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import MAX_REPAIR_ATTEMPTS

    async def fake_summarize(state, *, deps):
        return {
            "summary": {"x": 1},
            "call_count": state.get("call_count", 0) + 1,
            "llm_calls": [
                {"request_id": state["request_id"], "status": "ok", "attempt_trigger": "graph_node"}
            ],
        }

    async def always_invalid(state, *, deps):
        return {"validation_errors": ["forced"]}

    async def fake_repair(state, *, deps):
        # Mirror the real repair node: advance the budget, accumulate one record per
        # attempt, and raise CallBudgetExceeded once the budget is exhausted.
        attempts = state.get("repair_attempts", 0) + 1
        if attempts > MAX_REPAIR_ATTEMPTS:
            raise CallBudgetExceeded("exhausted")
        return {
            "repair_attempts": attempts,
            "llm_calls": [
                {
                    "request_id": state["request_id"],
                    "status": "error",
                    "attempt_trigger": "graph_node",
                }
            ],
        }

    patched = dict(graph_mod._NODES)
    patched["summarize"] = fake_summarize
    patched["validate"] = always_invalid
    patched["repair"] = fake_repair
    monkeypatch.setattr(graph_mod, "_NODES", patched)
    monkeypatch.setattr(lifecycle_mod, "persist_request_failure", AsyncMock())

    inserted: list[dict] = []
    m = MagicMock()
    llm_repo = MagicMock()
    llm_repo.async_insert_llm_call = AsyncMock(side_effect=lambda record: inserted.append(record))
    deps = SummarizeDeps(
        llm_client=m,
        retrieval=m,
        extraction=m,
        stream_sink=m,
        summaries=m,
        requests=m,
        summary_index=m,
        llm_repo=llm_repo,
    )

    graph = build_summarize_graph(deps=deps, checkpointer=InMemorySaver())
    out = await run_summarize_graph(
        graph=graph, deps=deps, correlation_id="corr-acc", request_id=99, lang="en"
    )

    assert "Error ID: corr-acc" in out["error"]
    # 1 summarize(ok) + MAX_REPAIR_ATTEMPTS repair(error) rows all reached the DB.
    assert len(inserted) == 1 + MAX_REPAIR_ATTEMPTS
    assert inserted[0]["status"] == "ok"  # chronological: summarize first
    assert all(r["request_id"] == 99 for r in inserted)
    assert sum(1 for r in inserted if r["status"] == "error") == MAX_REPAIR_ATTEMPTS


async def test_di_compiles_with_default_in_memory_checkpointer() -> None:
    pytest.importorskip("langgraph")
    from app.di.graphs import build_summarize_graph_app

    graph = build_summarize_graph_app(deps=_real_deps())
    assert graph is not None


def test_build_summarize_graph_app_forwards_injected_checkpointer(monkeypatch) -> None:
    """An injected checkpointer must reach the compiled graph (audit #15).

    Regression: ``build_summarize_graph_app`` previously only ever saw ``None`` from
    its callers, so the graph always compiled with the in-memory saver even when the
    Postgres ``CheckpointerRuntime.saver`` was available. When a checkpointer is
    passed it must be forwarded verbatim (never replaced by an ``InMemorySaver``).
    """
    import app.di.graphs as graphs_mod

    captured: dict[str, object] = {}

    def _capture(*, deps, checkpointer):
        captured["checkpointer"] = checkpointer
        return MagicMock()

    monkeypatch.setattr(graphs_mod, "build_summarize_graph", _capture)
    sentinel = object()
    graphs_mod.build_summarize_graph_app(deps=_real_deps(), checkpointer=sentinel)

    assert captured["checkpointer"] is sentinel


def test_assemble_graph_url_processor_threads_checkpointer(monkeypatch) -> None:
    """``assemble_graph_url_processor`` must thread its checkpointer to the compiled graph.

    Regression for audit #15: the assemble seam compiled the graph with
    ``build_summarize_graph_app(deps=deps)`` and dropped any checkpointer on the
    floor, so the Postgres saver injected at the runtime seam never reached the
    graph. The ``checkpointer`` kwarg must flow through to ``build_summarize_graph_app``.
    """
    import app.di.graphs as graphs_mod

    captured: dict[str, object] = {}

    def _capture_app(*, deps, checkpointer=None):
        captured["checkpointer"] = checkpointer
        return MagicMock()

    monkeypatch.setattr(graphs_mod, "build_summarize_graph_app", _capture_app)
    # Stub the facade builder so we exercise only the assemble -> app seam.
    monkeypatch.setattr(graphs_mod, "build_graph_url_processor", lambda **_kw: MagicMock())

    cfg = MagicMock()
    cfg.runtime.summarize_rag_enabled = False
    cfg.runtime.rag_top_k = 5
    cfg.model_routing.enabled = False
    sentinel = object()

    graphs_mod.assemble_graph_url_processor(
        cfg=cfg,
        db=MagicMock(),
        content_extractor=MagicMock(),
        cached_summary_responder=MagicMock(),
        summary_delivery=MagicMock(),
        post_summary_tasks=MagicMock(),
        response_formatter=MagicMock(),
        audit_func=MagicMock(),
        summarization_runtime=MagicMock(),
        llm_client=MagicMock(),
        request_repo=MagicMock(),
        summary_repo=MagicMock(),
        crawl_result_repo=MagicMock(),
        llm_repo=MagicMock(),
        checkpointer=sentinel,
    )

    assert captured["checkpointer"] is sentinel
