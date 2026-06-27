"""Structured Russian translation of a finished summary.

Produces a full Russian counterpart of an already-shaped summary dict so the bot
can deliver every field (not only the TL;DR) in both languages. The translation
reuses the same Instructor structured-output path the summarize graph trusts
(``llm_client.chat_structured`` against ``SummaryModel``), so the result is a
validated, render-ready summary dict rather than free-form text.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.application.ports.llm_client import LLMClientProtocol

logger = get_logger(__name__)

# Fields that are never rendered to the user and only add tokens to the
# translation request -- dropped from the LLM input. The structured response is
# a full SummaryModel regardless; these simply keep the prompt lean.
_NON_RENDERED_INPUT_FIELDS = (
    "semantic_chunks",
    "seo_keywords",
    "query_expansion_keywords",
    "semantic_boosters",
    "summary_quality",
    "article_id",
)

_RU_TRANSLATION_SYSTEM = (
    "You are a professional translator. You receive a structured article summary "
    "as JSON. Return the SAME structured summary translated into natural, fluent "
    "Russian. Rules: translate every human-readable text field (titles, TL;DR, "
    "key ideas, highlights, quotes, questions, insights, quality notes, stat "
    "labels, categories, tags) into Russian. Do NOT translate or alter URLs, "
    "numbers, dates, enum values (source_type, temporal_freshness, "
    "hallucination_risk, readability method/level), or proper nouns and entity "
    "names that are normally kept in their original form. Preserve the structure, "
    "keys, and list ordering exactly. Set tldr_ru to the Russian TL;DR."
)


def build_ru_translation_messages(summary: dict[str, Any]) -> list[dict[str, str]]:
    """Build the chat messages that ask the LLM to translate ``summary`` to Russian."""
    lean = {k: v for k, v in summary.items() if k not in _NON_RENDERED_INPUT_FIELDS}
    summary_json = json.dumps(lean, ensure_ascii=False, indent=2)
    return [
        {"role": "system", "content": _RU_TRANSLATION_SYSTEM},
        {
            "role": "user",
            "content": (
                "Translate the following article summary into Russian, returning the "
                "complete structured summary:\n\n" + summary_json
            ),
        },
    ]


async def translate_summary_to_ru_struct(
    *,
    llm_client: LLMClientProtocol,
    summary: dict[str, Any],
    cfg: Any,
    correlation_id: str | None = None,
    req_id: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Translate a shaped summary dict into a Russian shaped dict.

    Returns ``(ru_summary, call_meta)`` where ``call_meta`` carries the LLM-call
    fields (model / token counts / cost / latency) the caller persists to
    ``llm_calls``. Returns ``(None, None)`` when translation is not possible (no
    client, empty input, or the LLM call fails); callers treat that as "fall back
    to the existing behavior" -- it is never fatal.
    """
    if llm_client is None or not isinstance(summary, dict) or not summary:
        return None, None

    from app.core.summary_schema import SummaryModel

    messages = build_ru_translation_messages(summary)
    openrouter_cfg = getattr(cfg, "openrouter", None)
    temperature = float(getattr(openrouter_cfg, "temperature", 0.2) or 0.2)
    # Translation (paraphrase + re-validate JSON) is well within flash-model
    # capability, so prefer the cheaper/faster model when one is configured.
    model_override = getattr(openrouter_cfg, "flash_model", None) or None
    try:
        result = await llm_client.chat_structured(
            messages,
            response_model=SummaryModel,
            temperature=temperature,
            request_id=req_id,
            model_override=model_override,
        )
    except Exception as exc:
        raise_if_cancelled(exc)
        logger.warning(
            "summary_ru_struct_translation_failed",
            extra={"cid": correlation_id, "error": str(exc)},
        )
        return None, None

    parsed = getattr(result, "parsed", None)
    if parsed is None:
        return None, None
    translated = parsed.model_dump()
    # Carry over the canonical URL / domain so the Russian card can still render
    # the source link and cover -- these are identifiers the translator may drop.
    for key in ("canonical_url", "metadata"):
        if not translated.get(key) and summary.get(key):
            translated[key] = summary[key]

    call_meta = {
        "model": getattr(result, "model_used", None),
        "tokens_prompt": getattr(result, "tokens_prompt", None),
        "tokens_completion": getattr(result, "tokens_completion", None),
        "cost_usd": getattr(result, "cost_usd", None),
        "latency_ms": getattr(result, "latency_ms", None),
    }
    return translated, call_meta
