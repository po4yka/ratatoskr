"""``validate`` node -- check the summary against the contract (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node
from app.application.graphs.summarize.nodes._context import load_source_text
from app.core.summary_contract import validate_and_shape_summary
from app.core.summary_contract_impl.quality_metadata import (
    infer_source_coverage,
    merge_summary_quality_metadata,
)

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("validate")
async def validate(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Validate + shape ``state['summary']`` against the strict summary contract.

    On success: replace ``summary`` with the canonical shaped payload and clear
    ``validation_errors`` (router -> enrich). On a contract ``ValidationError``:
    populate ``validation_errors`` (router -> repair). A missing summary is also
    invalid: routing it through repair guarantees the run either produces a real
    payload or reaches the terminal failure sink instead of leaving the Request in
    ``processing``.
    """
    summary = state.get("summary")
    if not summary:
        return {"validation_errors": ["Summary is missing"]}
    try:
        shaped = validate_and_shape_summary(summary)
    except Exception as exc:
        return {"validation_errors": [str(exc)]}
    source_text = state.get("content_for_summary") or await load_source_text(state, deps)
    inferred_source_coverage = infer_source_coverage(
        content_text=source_text,
        content_source=state.get("content_source"),
    )
    merge_summary_quality_metadata(
        shaped,
        source_coverage=inferred_source_coverage if inferred_source_coverage != "unknown" else None,
        repair_attempted=True if state.get("repair_attempts", 0) > 0 else None,
        repair_succeeded=True if state.get("repair_attempts", 0) > 0 else None,
    )
    return {"summary": shaped, "validation_errors": []}
