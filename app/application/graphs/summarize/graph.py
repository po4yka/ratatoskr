"""Summarize ``StateGraph`` assembly + invocation (ADR-0010/0011/0015).

This module (with :mod:`app.di.graphs`) is the ONLY langgraph-coupled surface.
langgraph is imported **lazily inside the functions** so the module stays
importable in the import-linter / mypy / unit-test CI envs, which do not install
the optional ``graph`` extra (an ``app.*`` import must never require it).

Contract:

- Nodes are bound to their port-typed ``SummarizeDeps`` via ``functools.partial``
  at build time -- deps live in graph *config*, never in serializable state
  (ADR-0011).
- ``thread_id == correlation_id`` and ``recursion_limit`` are set PER-INVOCATION
  (see :func:`run_summarize_graph`), not at ``compile``.
- All failures (node exception, ``GraphRecursionError``, ``CallBudgetExceeded``)
  route to the single terminal-failure path in :mod:`lifecycle` -- no parallel
  error path.
"""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize import nodes
from app.application.graphs.summarize.lifecycle import (
    REASON_GRAPH_CALL_BUDGET_EXCEEDED,
    REASON_GRAPH_NODE_FAILURE,
    REASON_GRAPH_RECURSION_LIMIT,
    CallBudgetExceeded,
    route_terminal_failure,
)
from app.application.graphs.summarize.state import MAX_REPAIR_ATTEMPTS, SummarizeState

if TYPE_CHECKING:
    from collections.abc import Hashable

    from app.application.graphs.summarize.deps import SummarizeDeps

logger = logging.getLogger(__name__)

# Default per-invocation langgraph recursion limit (langgraph's own default is 25).
# It is an INDEPENDENT backstop to the repair budget: it must stay comfortably
# above the worst-case super-step count (linear spine + 2*MAX_REPAIR_ATTEMPTS for
# the validate<->repair loop) so the repair budget is the PRIMARY terminator and
# the recursion limit only catches a runaway. Both route to the same terminal path.
DEFAULT_RECURSION_LIMIT = 25
assert DEFAULT_RECURSION_LIMIT > 10 + 2 * MAX_REPAIR_ATTEMPTS  # spine(10) + repair loop headroom

# Conditional routing out of ``validate``: router return value -> next node. Shared
# by ``_route_after_validate`` and the ``add_conditional_edges`` map so the two
# cannot drift (a CI-safe test pins the router outputs to these keys).
# dict[Hashable, str] (not dict[str, str]) to match langgraph's add_conditional_edges
# signature -- dict keys are invariant, so a str-keyed dict would not satisfy it.
VALIDATE_ROUTES: dict[Hashable, str] = {"enrich": "enrich", "repair": "repair"}

# Linear spine of the pipeline (validate's outgoing edges are conditional).
_LINEAR_EDGES: tuple[tuple[str, str], ...] = (
    ("ingest", "extract"),
    ("extract", "ground"),
    ("ground", "build_prompt"),
    ("build_prompt", "summarize"),
    ("summarize", "validate"),
    ("repair", "validate"),
    ("enrich", "persist"),
    ("persist", "notify"),
)

# node-name -> node coroutine, in declaration order.
_NODES: dict[str, Any] = {
    "ingest": nodes.ingest,
    "extract": nodes.extract,
    "ground": nodes.ground,
    "build_prompt": nodes.build_prompt,
    "summarize": nodes.summarize,
    "validate": nodes.validate,
    "repair": nodes.repair,
    "enrich": nodes.enrich,
    "persist": nodes.persist,
    "notify": nodes.notify,
}


def _route_after_validate(state: SummarizeState) -> str:
    """Conditional edge: valid summary -> ``enrich``; otherwise -> ``repair``.

    Repair-budget exhaustion is signalled by the repair node raising
    ``CallBudgetExceeded`` (routed to the terminal path), so this router only
    distinguishes valid from invalid.
    """
    if state.get("validation_errors"):
        return "repair"
    return "enrich"


def build_summarize_graph(*, deps: SummarizeDeps, checkpointer: Any) -> Any:
    """Assemble and compile the summarize ``StateGraph``.

    langgraph is imported here (lazily) -- this is the framework seam. Returns a
    compiled graph; ``thread_id`` / ``recursion_limit`` are supplied per-invocation
    by :func:`run_summarize_graph`, not here.
    """
    from langgraph.graph import END, START, StateGraph

    builder = StateGraph(SummarizeState)
    for node_name, node_fn in _NODES.items():
        # Bind port-typed deps via partial: deps stay in config, never in state.
        builder.add_node(node_name, functools.partial(node_fn, deps=deps))

    builder.add_edge(START, "ingest")
    for src, dst in _LINEAR_EDGES:
        builder.add_edge(src, dst)
    builder.add_conditional_edges("validate", _route_after_validate, VALIDATE_ROUTES)
    builder.add_edge("notify", END)

    return builder.compile(checkpointer=checkpointer)


async def run_summarize_graph(
    *,
    graph: Any,
    deps: SummarizeDeps,
    correlation_id: str,
    request_id: int,
    lang: str,
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
) -> dict[str, Any]:
    """Invoke a compiled summarize graph for one request.

    ``thread_id == correlation_id`` (sacred, ADR-0011) and ``recursion_limit`` are
    set here, per-invocation. Any failure -- a node exception, langgraph's
    ``GraphRecursionError``, or ``CallBudgetExceeded`` (all subclasses of
    ``Exception``) -- is routed to the single terminal-failure path; cancellation
    (``BaseException``) is never swallowed. On failure returns
    ``{"error": "<message>", ...}``; on success returns the final graph state.
    """
    initial_state: SummarizeState = {
        "correlation_id": correlation_id,
        "request_id": request_id,
        "lang": lang,
        "grounding_ids": [],
        "summary": {},
        "validation_errors": [],
        "repair_attempts": 0,
        "call_count": 0,
    }
    config: dict[str, Any] = {
        "configurable": {"thread_id": correlation_id},
        "recursion_limit": recursion_limit,
    }
    try:
        return await graph.ainvoke(initial_state, config=config)
    except Exception as exc:
        # Single terminal sink (ADR-0011): a node exception, GraphRecursionError,
        # and CallBudgetExceeded all route here. BaseException (e.g. cancellation)
        # is deliberately NOT caught.
        if isinstance(exc, CallBudgetExceeded):
            reason_code = REASON_GRAPH_CALL_BUDGET_EXCEEDED
        elif type(exc).__name__ == "GraphRecursionError":
            # Matched by name, not isinstance, to avoid importing langgraph at
            # module scope (the no-graph-extra import invariant, ADR-0018).
            reason_code = REASON_GRAPH_RECURSION_LIMIT
        else:
            reason_code = REASON_GRAPH_NODE_FAILURE
        logger.warning(
            "summarize_graph_terminal_failure",
            extra={"correlation_id": correlation_id, "error_type": type(exc).__name__},
        )
        # initial_state is used deliberately: correlation_id / request_id are
        # invocation-fixed and sacred, so recovering (possibly partial) graph state
        # is unnecessary for the Error ID + persistence target.
        message = await route_terminal_failure(initial_state, deps, exc, reason_code=reason_code)
        return {
            "error": message,
            "correlation_id": correlation_id,
            "request_id": request_id,
        }
