"""``build_prompt`` node -- assemble the system + user prompt (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.nodes._span import graph_node

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("build_prompt")
async def build_prompt(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Assemble the contract system prompt + user prompt for the chosen language.

    STUB (T5/T7): full base-prompt assembly (en/ru lockstep, token-aware content
    prep) lands in T7. T6 owns the grounding seam ONLY: the ``ground`` node writes
    an anti-contamination block into ``grounding_block``; build_prompt concatenates
    it onto the system prompt. When RAG is off (or no hits), ``grounding_block`` is
    empty and this returns no update -- so the assembled prompt is byte-identical
    to the no-RAG path (flag-off parity, ADR-0018).
    """
    block = (state.get("grounding_block") or "").strip()
    if not block:
        return {}
    base = (state.get("system_prompt") or "").rstrip()
    combined = f"{base}\n\n{block}" if base else block
    return {"system_prompt": combined}
