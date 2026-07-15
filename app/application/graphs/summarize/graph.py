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
import inspect
import logging
from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize import nodes
from app.application.graphs.summarize.lifecycle import (
    REASON_GRAPH_CALL_BUDGET_EXCEEDED,
    REASON_GRAPH_NODE_FAILURE,
    REASON_GRAPH_RECURSION_LIMIT,
    CallBudgetExceeded,
    notification_type_for_exception,
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


def build_initial_state(
    *,
    correlation_id: str,
    request_id: int | None,
    lang: str,
    input_url: str = "",
    source_text: str = "",
    user_scope: str | None = None,
    environment: str | None = None,
    stream: bool = False,
    two_pass_eligible: bool = False,
) -> SummarizeState:
    """The per-invocation initial graph state (serializable primitives only).

    Shared by :func:`run_summarize_graph` (ainvoke) and the streaming driver in
    :mod:`app.di.graphs` so the two construct byte-identical state -- no stream
    buffer / token field ever appears here (ADR-0011/0017). ``stream`` is a plain
    mode flag (NOT a buffer): the streaming runner sets it so the summarize node
    takes the token-streaming LLM path; the ainvoke path leaves it False so T9
    parity stays byte-identical.

    ``source_text`` is the content-only entrypoint (T9 facade ``summarize``): the 4
    pre-extracted callers (api/background/handlers, rss) supply content directly
    with an empty ``input_url`` so the ``extract`` node no-ops (it returns ``{}``
    when no URL is settled) and leaves the pre-provided ``source_text`` untouched.
    Defaults to ``""`` so the URL path stays byte-identical (extract overwrites it).
    The same content-only callers pass ``request_id=None`` (no request row): the
    persist node short-circuits every DB write when it is ``None`` rather than
    INSERTing a Summary against ``requests.id`` that does not exist (FK violation).
    """
    initial_state: SummarizeState = {
        "correlation_id": correlation_id,
        "request_id": request_id,
        "lang": lang,
        "input_url": input_url,
        "source_text": source_text,
        "grounding_ids": [],
        "grounding_block": "",
        "summary": {},
        "validation_errors": [],
        "repair_attempts": 0,
        "call_count": 0,
        "llm_calls": [],
        "stream": stream,
        "two_pass_eligible": two_pass_eligible,
    }
    if user_scope is not None:
        initial_state["user_scope"] = user_scope
    if environment is not None:
        initial_state["environment"] = environment
    return initial_state


def invocation_config(*, correlation_id: str, recursion_limit: int) -> dict[str, Any]:
    """Per-invocation langgraph config: ``thread_id == correlation_id`` (sacred)."""
    return {
        "configurable": {"thread_id": correlation_id},
        "recursion_limit": recursion_limit,
    }


async def recover_accumulated_llm_calls(graph: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover the ``llm_calls`` accumulated in the graph checkpoint after a failure.

    The success-path writer of ``state['llm_calls']`` is the ``persist`` node, which
    a terminal failure (``CallBudgetExceeded``, ``GraphRecursionError``, a node
    exception) never reaches -- so every ``summarize`` / ``repair`` LLM call
    accumulated in state would be dropped, violating persist-everything (rule 3).
    The graph is ALWAYS compiled with a checkpointer (the Postgres saver, or the
    ``InMemorySaver`` fallback), so the last committed super-step state is
    recoverable here, in the langgraph-coupled layer -- ``lifecycle.py`` stays
    framework-free and receives the plain list.

    Best-effort: any failure to read the checkpoint yields ``[]`` so the terminal
    path still marks the request ERROR. Disjoint from a node's
    ``exc.llm_failure_records`` (a node commits to the checkpoint XOR attaches to
    the raised exception), so the terminal handler persists the union without
    double-counting.
    """
    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        logger.warning(
            "summarize_graph_llm_calls_recovery_failed",
            extra={"thread_id": config.get("configurable", {}).get("thread_id")},
            exc_info=True,
        )
        return []
    values = getattr(snapshot, "values", None)
    if not isinstance(values, dict):
        return []
    records = values.get("llm_calls")
    return list(records) if isinstance(records, list) else []


async def cleanup_checkpoint_thread(graph: Any, config: dict[str, Any]) -> None:
    """Best-effort delete of one terminal graph thread's checkpoint state.

    Callers invoke this only after a successful run or after terminal-failure
    recovery and persistence. Cancellation deliberately bypasses cleanup so an
    interrupted in-flight run remains resumable.
    """
    thread_id = config.get("configurable", {}).get("thread_id")
    checkpointer = getattr(graph, "checkpointer", None)
    if not isinstance(thread_id, str) or not thread_id or checkpointer is None:
        return
    try:
        delete = getattr(checkpointer, "adelete_thread", None)
        if not callable(delete):
            delete = getattr(checkpointer, "delete_thread", None)
        if not callable(delete):
            return
        result = delete(thread_id)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.warning(
            "summarize_graph_checkpoint_cleanup_failed",
            extra={"thread_id": thread_id},
            exc_info=True,
        )


def reason_code_for_exception(exc: BaseException) -> str:
    """Map a terminal exception to its failure reason code (single mapping)."""
    if isinstance(exc, CallBudgetExceeded):
        return REASON_GRAPH_CALL_BUDGET_EXCEEDED
    # Matched by name, not isinstance, to avoid importing langgraph at module
    # scope (the no-graph-extra import invariant, ADR-0018).
    if type(exc).__name__ == "GraphRecursionError":
        return REASON_GRAPH_RECURSION_LIMIT
    return REASON_GRAPH_NODE_FAILURE


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
    request_id: int | None,
    lang: str,
    input_url: str = "",
    source_text: str = "",
    user_scope: str | None = None,
    environment: str | None = None,
    two_pass_eligible: bool = True,
    recursion_limit: int = DEFAULT_RECURSION_LIMIT,
) -> dict[str, Any]:
    """Invoke a compiled summarize graph for one request.

    ``thread_id == correlation_id`` (sacred, ADR-0011) and ``recursion_limit`` are
    set here, per-invocation. Any failure -- a node exception, langgraph's
    ``GraphRecursionError``, or ``CallBudgetExceeded`` (all subclasses of
    ``Exception``) -- is routed to the single terminal-failure path; cancellation
    (``BaseException``) is never swallowed. On failure returns
    ``{"error": "<message>", ...}``; on success returns the final graph state.

    ``source_text`` seeds pre-extracted content for callers that have a request row
    but skip the extract node (voice transcripts / uploaded text via
    ``summarize_text_request``): passed with an empty ``input_url`` so extract
    no-ops and leaves the seed untouched. Defaults to ``""`` so the URL path (extract
    fetches the content) is byte-identical.

    ``two_pass_eligible`` gates the optional enrich node (AND-ed with
    ``config.two_pass_enabled``, default False). Defaults ``True`` for the URL flow;
    pre-extracted text callers pass ``False`` to keep enrichment scoped to the URL
    path (audit #20).
    """
    initial_state = build_initial_state(
        correlation_id=correlation_id,
        request_id=request_id,
        lang=lang,
        input_url=input_url,
        source_text=source_text,
        user_scope=user_scope,
        environment=environment,
        two_pass_eligible=two_pass_eligible,
    )
    config = invocation_config(correlation_id=correlation_id, recursion_limit=recursion_limit)
    try:
        final_state = await graph.ainvoke(initial_state, config=config)
    except Exception as exc:
        # Single terminal sink (ADR-0011): a node exception, GraphRecursionError,
        # and CallBudgetExceeded all route here. BaseException (e.g. cancellation)
        # is deliberately NOT caught.
        reason_code = reason_code_for_exception(exc)
        logger.warning(
            "summarize_graph_terminal_failure",
            extra={"correlation_id": correlation_id, "error_type": type(exc).__name__},
        )
        # initial_state carries the invocation-fixed, sacred correlation_id /
        # request_id -- sufficient for the Error ID + failure snapshot. The
        # accumulated llm_calls are NOT in it (initial_state seeds them empty), so
        # recover them from the checkpoint and hand them to the terminal sink;
        # otherwise every summarize/repair call on a failed run is dropped (rule 3).
        recovered_llm_calls = await recover_accumulated_llm_calls(graph, config)
        try:
            message = await route_terminal_failure(
                initial_state,
                deps,
                exc,
                reason_code=reason_code,
                recovered_llm_calls=recovered_llm_calls,
            )
        finally:
            await cleanup_checkpoint_thread(graph, config)
        return {
            "error": message,
            "notification_type": notification_type_for_exception(exc),
            "correlation_id": correlation_id,
            "request_id": request_id,
        }
    await cleanup_checkpoint_thread(graph, config)
    return final_state
