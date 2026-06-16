"""Per-node OTel span helper for summarize-graph nodes (ADR-0011).

The ``@graph_node`` decorator wraps each ``async def node(state, *, deps)`` so it
runs inside a named OTel span carrying the canonical node name and the request
``correlation_id`` -- reusing :mod:`app.observability.otel` rather than building
spans by hand in every node. langgraph-free: the decorator only touches the
state dict and the tracer.
"""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, Any

from app.observability.attributes import (
    GRAPH_NAME,
    GRAPH_NODE,
    GRAPH_THREAD_ID,
    REQUEST_CORRELATION_ID,
)
from app.observability.otel import get_tracer, set_correlation_id_attr

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState

    # Node signature is ``async def(state, *, deps) -> dict`` (deps keyword-only),
    # which the simple Callable form cannot express -- keep it broad.
    NodeFn = Callable[..., Awaitable[dict[str, Any]]]

# Single source for the graph's logical name (tracer suffix + graph.name span attr).
_GRAPH_NAME = "summarize"
_tracer = get_tracer(f"ratatoskr.graph.{_GRAPH_NAME}")


def graph_node(name: str) -> Callable[[NodeFn], NodeFn]:
    """Wrap a node so it executes inside a ``graph.node.<name>`` OTel span.

    The span carries :data:`GRAPH_NODE` and the request ``correlation_id``
    (:data:`REQUEST_CORRELATION_ID`); ``set_correlation_id_attr`` propagates the
    correlation id onto the active span. ``start_as_current_span`` is a *sync*
    context manager (no-op when OTel is disabled), so it is entered with ``with``.
    """

    def decorate(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        async def wrapper(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
            correlation_id = state.get("correlation_id") or ""
            with _tracer.start_as_current_span(
                f"graph.node.{name}",
                attributes={
                    GRAPH_NAME: _GRAPH_NAME,
                    GRAPH_NODE: name,
                    # thread_id == correlation_id (sacred, ADR-0011).
                    GRAPH_THREAD_ID: correlation_id,
                    REQUEST_CORRELATION_ID: correlation_id,
                },
            ):
                set_correlation_id_attr(correlation_id or None)
                return await fn(state, deps=deps)

        return wrapper

    return decorate
