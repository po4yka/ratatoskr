"""``build_prompt`` node -- assemble the system + user prompt (ADR-0015)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.graphs.summarize.deps import SummarizeConfig
from app.application.graphs.summarize.nodes._span import graph_node
from app.application.services.summarization.graph_prompt import (
    build_multimodal_user_content,
    build_summary_user_prompt,
    filter_valid_images,
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

    GAP 1 (content-aware tier routing): after long-context routing, when
    ``config.routing_enabled`` is True and ``deps.model_router`` is set, classifies
    the content tier via ``classify_content`` (app.core -- legal from this layer) and
    resolves a tier-specific model override. Mirrors legacy
    ``PureSummaryService.summarize`` lines 87-98 exactly.
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

    # Article-vision routing (audit #2): decide vision BEFORE tier routing, mirroring
    # the legacy ``_prepare_summary_content`` priority (vision < long-context, but
    # vision > content-tier). The extract node lifted the article's image candidates
    # into ``state['images']``; we re-filter them through the SAME validator the
    # legacy path used so model selection and the multimodal message share one set.
    valid_images: list[str] = []
    use_vision = False
    if (
        config is not None
        and config.article_vision_enabled
        and config.vision_model
        and state.get("images")
    ):
        valid_images = filter_valid_images(state.get("images"))
        use_vision = len(valid_images) >= max(1, config.article_vision_min_images)
    # Vision override only when long-context did not already pin a model (long-context
    # wins, exactly as legacy line 446 overrode the vision model for oversized content).
    # ``use_vision`` is only True when ``config.vision_model`` is truthy (guarded above).
    if use_vision and model_override is None and config is not None:
        model_override = config.vision_model

    # GAP 1: content-aware tier routing (lower priority than long-context AND vision).
    # Mirrors pure_summary_service.py:87-98 verbatim.
    if (
        model_override is None
        and config is not None
        and config.routing_enabled
        and deps.model_router is not None
    ):
        from app.core.content_classifier import classify_content

        tier = classify_content(content_for_summary)
        model_override = deps.model_router(tier, len(content_for_summary))

    system_prompt = load_instructor_system_prompt(lang)
    if block:
        system_prompt = f"{system_prompt.rstrip()}\n\n{block}"

    user_prompt = build_summary_user_prompt(
        content_for_summary=content_for_summary, chosen_lang=lang
    )
    # Multimodal user message when vision is active; otherwise a plain text message.
    user_content: Any = (
        build_multimodal_user_content(user_prompt, valid_images) if use_vision else user_prompt
    )
    max_tokens = select_max_tokens(
        content_for_summary,
        configured_max=config.configured_max_tokens if config else None,
    )

    return {
        "system_prompt": system_prompt,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "content_for_summary": content_for_summary,
        "model_override": model_override or "",
        "max_tokens": int(max_tokens or 0),
    }
