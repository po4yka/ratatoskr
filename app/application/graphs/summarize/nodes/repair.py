"""``repair`` node -- re-prompt to fix contract-validation errors (ADR-0011/0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.lifecycle import CallBudgetExceeded
from app.application.graphs.summarize.nodes._span import graph_node
from app.application.graphs.summarize.state import MAX_REPAIR_ATTEMPTS

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("repair")
async def repair(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Re-prompt the LLM to repair contract-validation errors, bounded by budget.

    The validate -> repair -> validate loop is bounded by ``MAX_REPAIR_ATTEMPTS``
    (and, independently, langgraph's per-invocation ``recursion_limit``). When the
    repair budget is exhausted this raises ``CallBudgetExceeded``, which the runner
    routes to the single terminal-failure path (no parallel error path, ADR-0011).

    STUB (T5): only the budget bookkeeping is real; the instructor repair call
    lands in T7.
    """
    attempts = state.get("repair_attempts", 0) + 1
    if attempts > MAX_REPAIR_ATTEMPTS:
        raise CallBudgetExceeded(f"repair budget exhausted after {MAX_REPAIR_ATTEMPTS} attempts")
    return {"repair_attempts": attempts}
