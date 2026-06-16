"""LLM execution for the summarize graph (ADR-0006/0015).

Ports ``PureSummaryService._summarize_with_instructor`` / ``_classify_sticky_error``
and ``enrich_two_pass`` into the application layer (reachable by the graph
``summarize`` / ``enrich`` nodes without importing ``app.adapters``). Uses the
``LLMClientProtocol`` port + ``app.core`` helpers only. Legacy stays untouched
during the strangler-fig window; T9 deletes it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.core.call_status import CallStatus
from app.core.content_cleaner import detect_prompt_injection_patterns
from app.core.json_utils import dumps as json_dumps
from app.core.json_utils import extract_json
from app.core.lang import LANG_RU
from app.core.summary_contract_impl.quality_metadata import merge_summary_quality_metadata

from app.application.services.summarization.graph_prompt import _PROMPTS_DIR
from app.prompts.file_cache import read_prompt_text

if TYPE_CHECKING:
    from app.application.ports.llm_client import LLMClientProtocol

logger = logging.getLogger(__name__)

# Substring-matched sticky error classes (first-match order is load-bearing).
_STICKY_ERROR_CLASSES = (
    "per_model_timeout",
    "repeated_truncation",
    "truncation_recovery_skipped_budget_tight",
)

# Core fields excluded from the enrichment LLM's view (it must not regenerate them).
_ENRICH_CORE_FIELDS = {
    "summary_250",
    "summary_1000",
    "tldr",
    "key_ideas",
    "topic_tags",
    "entities",
    "source_type",
}
# The 8 keys the enrichment pass may contribute (truthy-only merge).
_ENRICH_KEYS = (
    "answered_questions",
    "seo_keywords",
    "extractive_quotes",
    "highlights",
    "categories",
    "key_points_to_remember",
    "questions_answered",
    "topic_taxonomy",
)


def classify_sticky_error(exc: Exception) -> str | None:
    """Return the sticky-error class (substring match) or None (verbatim parity)."""
    text = str(exc)
    for label in _STICKY_ERROR_CLASSES:
        if label in text:
            return label
    return None


async def summarize_with_instructor(
    *,
    llm_client: LLMClientProtocol,
    messages: list[dict[str, Any]],
    source_content: str,
    max_tokens: int | None,
    model_override: str | None,
    temperature: float,
    max_retries: int,
    sticky_fallback_enabled: bool,
    structured_output_mode: str | None,
    correlation_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Structured summary via Instructor with the sticky-failure force-fallback retry.

    Verbatim parity with ``PureSummaryService._summarize_with_instructor``: at most
    one retry; on a sticky error with an active override, drop the override (fall
    back to the base-model cascade) and retry once. Raises ``ValueError`` on
    failure (the summarize node routes it to the single terminal-failure path).

    Returns ``(summary, call_meta)`` where ``call_meta`` carries the serializable
    llm-call fields (model / token counts) the persist node writes into
    ``llm_calls`` with ``attempt_trigger='graph_node'`` (persist-everything).
    """
    from app.core.summary_schema import SummaryModel

    result: Any = None
    last_error: Exception | None = None
    override_dropped = False

    for attempt in range(2):
        current_override = None if override_dropped else model_override
        try:
            result = await llm_client.chat_structured(
                messages,
                response_model=SummaryModel,
                max_retries=max_retries,
                temperature=temperature,
                max_tokens=max_tokens,
                model_override=current_override,
            )
            break
        except Exception as exc:
            last_error = exc
            sticky_class = classify_sticky_error(exc)
            if (
                sticky_fallback_enabled
                and sticky_class is not None
                and not override_dropped
                and attempt == 0
                and current_override is not None
            ):
                override_dropped = True
                logger.warning(
                    "summarize_sticky_failure_force_fallback",
                    extra={
                        "cid": correlation_id,
                        "failed_model": current_override,
                        "error_class": sticky_class,
                        "next_action": "drop_model_override",
                    },
                )
                continue
            logger.error(
                "summarize_graph_instructor_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            raise ValueError(f"Instructor LLM call failed: {exc}") from exc

    if result is None:
        logger.error(
            "summarize_graph_instructor_failed",
            extra={"cid": correlation_id, "error": str(last_error)},
        )
        raise ValueError(f"Instructor LLM call failed: {last_error}") from last_error

    summary = mark_prompt_injection_metadata(result.parsed.model_dump(), source_content)
    quality = summary.get("quality")
    merge_summary_quality_metadata(
        summary,
        model_used=result.model_used,
        structured_output_mode=structured_output_mode,
        prompt_injection_suspected=(
            quality.get("prompt_injection_suspected", False) if isinstance(quality, dict) else False
        ),
    )
    call_meta = {
        "model": result.model_used,
        "tokens_prompt": getattr(result, "tokens_prompt", None),
        "tokens_completion": getattr(result, "tokens_completion", None),
        "cost_usd": getattr(result, "cost_usd", None),
        "latency_ms": getattr(result, "latency_ms", None),
    }
    return summary, call_meta


def mark_prompt_injection_metadata(summary: dict[str, Any], source_content: str) -> dict[str, Any]:
    """Re-run injection detection on the source and flag it in ``summary['quality']``.

    Faithful re-expression of ``summary_request_factory.mark_prompt_injection_metadata``
    (adapter layer, unreachable from nodes): sets
    ``quality.prompt_injection_suspected`` and appends a critique note when flagged.
    """
    detection = detect_prompt_injection_patterns(source_content)
    quality = summary.get("quality")
    if not isinstance(quality, dict):
        quality = {}
        summary["quality"] = quality
    quality["prompt_injection_suspected"] = bool(getattr(detection, "suspected", False))
    if getattr(detection, "suspected", False):
        insights = summary.get("insights")
        if not isinstance(insights, dict):
            insights = {}
            summary["insights"] = insights
        critique = insights.get("critique")
        if not isinstance(critique, list):
            critique = []
            insights["critique"] = critique
        critique.append("Potential prompt injection detected in untrusted source content.")
    return summary


async def enrich_two_pass(
    *,
    llm_client: LLMClientProtocol,
    summary: dict[str, Any],
    content_text: str,
    chosen_lang: str,
    temperature: float,
    top_p: float | None,
    enrichment_max_tokens: int,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """Optional second enrichment pass (verbatim parity with ``enrich_two_pass``).

    Never raises: any failure (LLM error, parse failure, exception) returns the
    input ``summary`` unchanged. Merges only the 8 enrichment keys, and only when
    truthy.
    """
    try:
        lang_suffix = "ru" if chosen_lang == LANG_RU else "en"
        enrichment_prompt = read_prompt_text(_PROMPTS_DIR / f"enrichment_system_{lang_suffix}.txt")
        core_summary_text = json_dumps(
            {k: v for k, v in summary.items() if k in _ENRICH_CORE_FIELDS}, indent=2
        )
        user_content = (
            f"Respond in {'Russian' if chosen_lang == LANG_RU else 'English'}.\n\n"
            f"CORE SUMMARY (already generated, do not modify):\n{core_summary_text}\n\n"
            f"ORIGINAL CONTENT START\n{content_text[:30000]}\nORIGINAL CONTENT END"
        )
        messages = [
            {"role": "system", "content": enrichment_prompt},
            {"role": "user", "content": user_content},
        ]
        llm_result = await llm_client.chat(
            messages,
            response_format={"type": "json_object"},
            max_tokens=enrichment_max_tokens,
            temperature=temperature,
            top_p=top_p,
            request_id=None,
        )
        if llm_result.status != CallStatus.OK:
            logger.warning(
                "two_pass_enrichment_failed",
                extra={"cid": correlation_id, "error": getattr(llm_result, "error_text", None)},
            )
            return summary
        enrichment = _parse_enrichment(llm_result)
        if not enrichment:
            logger.warning("two_pass_enrichment_parse_failed", extra={"cid": correlation_id})
            return summary
        for key in _ENRICH_KEYS:
            value = enrichment.get(key)
            if value:
                summary[key] = value
        logger.info(
            "two_pass_enrichment_merged",
            extra={
                "cid": correlation_id,
                "enriched_fields": [k for k in _ENRICH_KEYS if k in enrichment],
            },
        )
        return summary
    except Exception as exc:
        logger.warning(
            "two_pass_enrichment_error", extra={"cid": correlation_id, "error": str(exc)}
        )
        return summary


def _parse_enrichment(llm_result: Any) -> dict[str, Any] | None:
    """Extract the enrichment JSON dict from a chat result, or None."""
    text = getattr(llm_result, "response_text", None) or ""
    if not text.strip():
        return None
    try:
        parsed = extract_json(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None
