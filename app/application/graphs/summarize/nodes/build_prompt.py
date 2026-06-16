"""``build_prompt`` node -- assemble the system + user prompt (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.deps import SummarizeConfig
from app.application.graphs.summarize.nodes._span import graph_node
from app.application.services.summarization.graph_prompt import (
    build_summary_user_prompt,
    load_instructor_system_prompt,
    prepare_content_for_summary,
    select_max_tokens,
)

if TYPE_CHECKING:
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.graphs.summarize.state import SummarizeState


@graph_node("build_prompt")
async def build_prompt(state: SummarizeState, *, deps: SummarizeDeps) -> dict[str, Any]:
    """Assemble the instructor system prompt + user prompt for the chosen language.

    Full base-prompt assembly (ADR-0015): the LLM system message is the
    ``summary_system_{lang}_instructor.txt`` file (en/ru lockstep), with the T6
    grounding block concatenated when RAG produced one; the user prompt embeds the
    cleaned/long-context-routed source content; the dynamic output-token budget +
    optional model override are computed here and handed to ``summarize`` via state.

    When there is no extracted content (e.g. extract no-op'd), this preserves the
    T6 grounding-only seam exactly: empty block -> ``{}`` (byte-identical to the
    no-RAG path); a block alone -> concatenated onto any existing system prompt.
    """
    source_text = (state.get("source_text") or "").strip()
    block = (state.get("grounding_block") or "").strip()

    if not source_text:
        # No content to summarize -> T6 grounding-only behavior (flag-off parity).
        if not block:
            return {}
        base = (state.get("system_prompt") or "").rstrip()
        return {"system_prompt": f"{base}\n\n{block}" if base else block}

    lang = state.get("lang") or "en"
    config = deps.config if isinstance(deps.config, SummarizeConfig) else None

    content_for_summary, model_override = prepare_content_for_summary(source_text, config=config)

    system_prompt = load_instructor_system_prompt(lang)
    if block:
        system_prompt = f"{system_prompt.rstrip()}\n\n{block}"

    user_prompt = build_summary_user_prompt(
        content_for_summary=content_for_summary, chosen_lang=lang
    )
    max_tokens = select_max_tokens(
        content_for_summary,
        configured_max=config.configured_max_tokens if config else None,
    )

    return {
        "system_prompt": system_prompt,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "content_for_summary": content_for_summary,
        "model_override": model_override or "",
        "max_tokens": int(max_tokens or 0),
    }
