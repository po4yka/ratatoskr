"""The single terminal-failure path for the summarize graph (ADR-0011/0018).

Every failure mode -- a node raising, a langgraph ``GraphRecursionError``, or
``CallBudgetExceeded`` (repair/LLM budget exhausted) -- routes through
:func:`route_terminal_failure`. There is NO parallel error path: this reuses the
exact legacy terminal contract via
:func:`app.observability.failure_observability.persist_request_failure`
(``RequestStatus.ERROR`` + the structured failure snapshot) and produces the
user-facing ``Error ID: <correlation_id>`` message.

This module is langgraph-free (it must be importable in the import-linter / mypy
CI envs, which do not install the ``graph`` extra); the langgraph
``GraphRecursionError`` is caught in :mod:`graph` and handed here as a plain
exception.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.observability.failure_observability import persist_request_failure

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState

logger = logging.getLogger(__name__)

# Stage/component recorded on the failure snapshot for graph-originated terminal
# failures (queryable alongside the existing extraction reason codes).
_FAILURE_STAGE = "graph"
_FAILURE_COMPONENT = "summarize_graph"

# Reason codes, discriminated by failure mode so node faults, recursion-limit, and
# budget exhaustion stay queryable on the snapshot -- while STILL routing through
# this one helper (no parallel error path). The caller (graph.py) picks the code.
REASON_GRAPH_NODE_FAILURE = "GRAPH_NODE_FAILURE"
REASON_GRAPH_CALL_BUDGET_EXCEEDED = "GRAPH_CALL_BUDGET_EXCEEDED"
REASON_GRAPH_RECURSION_LIMIT = "GRAPH_RECURSION_LIMIT"


class CallBudgetExceeded(Exception):
    """The summarize run exhausted its per-request LLM/repair budget.

    Raised by the repair node (and, later, the summarize node) instead of looping
    forever; caught by the runner and routed to :func:`route_terminal_failure`.
    """


def error_id_message(correlation_id: str | None, request_id: int | None) -> str:
    """Build the user-facing terminal-error message (correlation id is sacred)."""
    error_id = correlation_id or (str(request_id) if request_id is not None else "unknown")
    return f"Processing failed (Error ID: {error_id}). Please try again."


async def route_terminal_failure(
    state: SummarizeState,
    deps: SummarizeDeps,
    error: BaseException,
    *,
    reason_code: str = REASON_GRAPH_NODE_FAILURE,
) -> str:
    """Persist the terminal failure and return the user-facing ``Error ID`` message.

    Single sink for all summarize-graph failures (ADR-0011): sets
    ``RequestStatus.ERROR`` via the shared persistence helper and never raises a
    second error path. Returns the message the caller surfaces to the user.
    """
    correlation_id = state.get("correlation_id")
    request_id = state.get("request_id")

    if request_id is not None:
        await persist_request_failure(
            request_repo=deps.requests,
            logger=logger,
            request_id=request_id,
            correlation_id=correlation_id,
            stage=_FAILURE_STAGE,
            component=_FAILURE_COMPONENT,
            reason_code=reason_code,
            error=error,
            retryable=False,
        )
    else:
        # No request row to attach the failure to (should not happen past ingest);
        # log with the correlation id so the failure is still traceable.
        logger.error(
            "summarize_graph_failure_without_request_id",
            extra={"correlation_id": correlation_id, "error": str(error)},
        )

    return error_id_message(correlation_id, request_id)
