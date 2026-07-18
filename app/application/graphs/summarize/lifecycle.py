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
from typing import TYPE_CHECKING, Any, cast

from app.observability.failure_observability import persist_request_failure

if TYPE_CHECKING:
    from collections.abc import Iterable

    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState
    from app.application.ports.requests import LLMCallRecord

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


# Substrings present in the exception messages raised by the extraction path
# (``app.adapters.content.content_extractor`` + the extract node, plus the
# academic extractor's ``AcademicPaperUnavailableError``) when the page could not
# be fetched / was paywalled / yielded no usable content. Matched
# case-insensitively so a content-acquisition failure is reported with the
# accurate "couldn't fetch the page" copy instead of the misleading
# "AI couldn't parse / repair failed" one.
_EXTRACTION_FAILURE_MARKERS = (
    "low-value content detected",
    "extraction failed",
    "content text is empty",
    "no usable content",
    "empty_after_cleaning",
    # AcademicPaperUnavailableError: "Academic paper unavailable (host=..., reason=
    # paywall/no_content/...)" -- paper behind a paywall/login or otherwise
    # unreachable; the LLM is never called.
    "academic paper unavailable",
)


def notification_type_for_exception(exc: BaseException) -> str:
    """Map a terminal exception to the user-facing ``send_error_notification`` type.

    LLM/repair/budget exhaustion keeps ``processing_failed`` ("the AI returned data
    that couldn't be parsed; repair was unsuccessful"). Extraction/content-fetch
    failures get ``empty_content`` ("couldn't retrieve the article -- blocked /
    paywall / non-text / server error"), which is what actually happened: the LLM
    was never reached. Anything else stays ``processing_failed`` (unchanged default).
    """
    if isinstance(exc, CallBudgetExceeded):
        return "processing_failed"
    if type(exc).__name__ == "GraphRecursionError":
        return "processing_failed"
    text = str(exc).lower()
    if any(marker in text for marker in _EXTRACTION_FAILURE_MARKERS):
        return "empty_content"
    return "processing_failed"


async def _drain_llm_calls(
    records: Iterable[Any] | None,
    deps: SummarizeDeps,
    *,
    correlation_id: str | None,
    request_id: int | None,
) -> None:
    """Best-effort write of accumulated/failure ``llm_calls`` rows (persist-everything).

    No-ops without a writer or a request row (content-only path -- every row would
    FK-violate ``requests.id``). One bad row is logged and skipped so it never
    blocks the remaining rows or the ERROR finalization.
    """
    if not records or deps.llm_repo is None or request_id is None:
        return
    for record in records:
        try:
            await deps.llm_repo.async_insert_llm_call(cast("LLMCallRecord", record))
        except Exception:
            logger.warning(
                "summarize_graph_failure_llm_call_persist_failed",
                extra={"correlation_id": correlation_id, "request_id": request_id},
                exc_info=True,
            )


async def route_terminal_failure(
    state: SummarizeState,
    deps: SummarizeDeps,
    error: BaseException,
    *,
    reason_code: str = REASON_GRAPH_NODE_FAILURE,
    recovered_llm_calls: Iterable[Any] | None = None,
) -> str:
    """Persist the terminal failure and return the user-facing ``Error ID`` message.

    Single sink for all summarize-graph failures (ADR-0011): sets
    ``RequestStatus.ERROR`` via the shared persistence helper and never raises a
    second error path. Returns the message the caller surfaces to the user.

    persist-everything (rule 3) is enforced across two DISJOINT sources of
    ``llm_calls`` rows, written in chronological order before the request is marked
    ERROR:

    - ``recovered_llm_calls`` -- the records already committed to the graph
      checkpoint (every successful ``summarize`` call + each ``repair`` attempt).
      The success-path writer is the ``persist`` node, which a terminal failure
      never reaches, so without this every accumulated call would be silently
      dropped -- most visibly the whole repair loop under ``CallBudgetExceeded``.
      The langgraph-coupled runner recovers them via ``aget_state`` and hands the
      plain list here (this module stays framework-free). Empty when the failure
      predates the first LLM call (e.g. an extract-stage fault).
    - GAP 3a ``error.llm_failure_records`` -- the FAILURE record the ``summarize``
      node attaches to the exception it RAISES (its channel writes are discarded,
      so this row is never in the checkpoint).

    The two sets never overlap: a node COMMITS its rows to the checkpoint (return)
    XOR ATTACHES them to the raised exception -- never both -- so the union is
    written without double-counting.
    """
    correlation_id = state.get("correlation_id")
    request_id = state.get("request_id")

    # Recovered checkpoint rows first (they happened first: summarize + repairs),
    # then the exception-attached failure row (the final raise), so DB insert order
    # -- and thus the repository-assigned ``attempt_index`` -- stays chronological.
    await _drain_llm_calls(
        recovered_llm_calls, deps, correlation_id=correlation_id, request_id=request_id
    )
    failure_records = getattr(error, "llm_failure_records", None)
    await _drain_llm_calls(
        failure_records, deps, correlation_id=correlation_id, request_id=request_id
    )

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
            raise_on_error=True,
        )
    else:
        # No request row to attach the failure to (should not happen past ingest);
        # log with the correlation id so the failure is still traceable.
        logger.error(
            "summarize_graph_failure_without_request_id",
            extra={"correlation_id": correlation_id, "error": str(error)},
        )

    return error_id_message(correlation_id, request_id)
