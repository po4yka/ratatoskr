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

    STUB (T5): returns no update. Prompt assembly (en/ru lockstep, grounding block
    concatenation, token-aware content prep) lands in T6/T7.
    """
    return {}
