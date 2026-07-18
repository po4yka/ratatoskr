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

from app.application.services.summarization.graph_llm_guard import (
    GraphLLMUsageBudgetExceeded,
)
from app.application.services.summarization.graph_prompt import _PROMPTS_DIR
from app.core.call_status import CallStatus
from app.core.content_cleaner import detect_prompt_injection_patterns
from app.core.json_utils import dumps as json_dumps, extract_json
from app.core.lang import LANG_RU
from app.core.llm_call_budget import LLMCallCapExceeded
from app.core.summary_contract_impl.quality_metadata import merge_summary_quality_metadata
from app.prompts.file_cache import read_prompt_text

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.application.ports.llm_client import LLMClientProtocol
    from app.application.services.summarization.graph_llm_guard import GraphLLMGuard

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

_STREAM_REPAIR_CANDIDATE_MAX_CHARS = 16_000


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
    request_id: int | None = None,
    guard: GraphLLMGuard | None = None,
    current_call_count: int = 0,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
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
    last_llm_result: Any = None  # carry the raw LLMCallResult for failure fidelity
    override_dropped = False
    call_count = current_call_count
    physical_attempts: list[dict[str, Any]] = []

    for attempt in range(2):
        current_override = None if override_dropped else model_override
        try:

            async def _call(
                effective_max_tokens: int | None,
                override: str | None = current_override,
            ) -> Any:
                return await llm_client.chat_structured(
                    messages,
                    response_model=SummaryModel,
                    max_retries=max_retries,
                    temperature=temperature,
                    max_tokens=effective_max_tokens,
                    request_id=request_id,
                    model_override=override,
                )

            if guard is None:
                call_count += 1
                result = await _call(max_tokens)
            else:
                result, call_count = await guard.invoke(
                    current_call_count=call_count,
                    request_id=request_id,
                    model=current_override,
                    max_tokens=max_tokens,
                    call=_call,
                )
            result_attempts = _physical_attempts(result, terminal_status="ok")
            physical_attempts.extend(result_attempts)
            call_count += max(0, len(result_attempts) - 1)
            break
        except (LLMCallCapExceeded, GraphLLMUsageBudgetExceeded):
            raise
        except Exception as exc:
            call_count = int(getattr(exc, "__llm_call_count__", call_count))
            failed_attempts = _exception_physical_attempts(exc)
            physical_attempts.extend(failed_attempts)
            call_count += max(0, len(failed_attempts) - 1)
            last_error = exc
            # Attempt to surface the raw LLMCallResult attached by the adapter.
            # The instructor adapter attaches ``__llm_result__`` on structured failures
            # so we can recover model/error_text/latency even from wrapped exceptions.
            last_llm_result = getattr(exc, "__llm_result__", None)
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
            err = ValueError(f"Instructor LLM call failed: {exc}")
            # Propagate the raw LLMCallResult so _tag_failure can build a
            # fidelity record (real model / error_text / latency).
            err.__llm_result__ = last_llm_result  # type: ignore[attr-defined]
            err.__llm_call_count__ = call_count  # type: ignore[attr-defined]
            err.__llm_physical_attempts__ = physical_attempts  # type: ignore[attr-defined]
            raise err from exc

    if result is None:
        logger.error(
            "summarize_graph_instructor_failed",
            extra={"cid": correlation_id, "error": str(last_error)},
        )
        err = ValueError(f"Instructor LLM call failed: {last_error}")
        err.__llm_result__ = last_llm_result  # type: ignore[attr-defined]
        err.__llm_call_count__ = call_count  # type: ignore[attr-defined]
        err.__llm_physical_attempts__ = physical_attempts  # type: ignore[attr-defined]
        raise err from last_error

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
    return summary, physical_attempts, call_count


async def summarize_streaming(
    *,
    llm_client: LLMClientProtocol,
    messages: list[dict[str, Any]],
    source_content: str,
    max_tokens: int | None,
    model_override: str | None,
    temperature: float,
    structured_output_mode: str | None,
    on_token: Callable[[str], Awaitable[None] | None],
    request_id: int | None = None,
    correlation_id: str | None = None,
    guard: GraphLLMGuard | None = None,
    current_call_count: int = 0,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Token-streaming summary path (ADR-0017): ONE ``chat(stream=True)`` call.

    The model is asked for a JSON object; ``on_token`` receives each raw text
    delta (dispatched as a ``summary_token`` event so the T8 bridge drives live
    section previews). The accumulated text is parsed tolerantly -- a malformed
    or partial object is handed forward as-is for the validate->repair loop to
    correct (no hard schema-validation here, unlike the structured path). The
    injection / quality post-processing is shared with
    :func:`summarize_with_instructor` so output shape stays consistent.

    Returns ``(summary, call_meta)``. Raises ``ValueError`` only when the provider
    call fails outright. An unparsable successful response becomes an explicitly
    invalid, bounded candidate so ``validate -> repair`` can correct it.

    The provider ``response_format`` honors ``structured_output_mode``: when it
    is ``"json_schema"`` the model is constrained to the strict summary schema
    (audit #19 -- previously this path hardcoded ``json_object`` and silently
    ignored the configured mode); otherwise it falls back to plain
    ``json_object`` via the same contract descriptor used by the structured path.
    """
    from app.core.summary_contract import get_summary_contract_descriptor

    response_format = get_summary_contract_descriptor().response_format(structured_output_mode)

    async def _call(effective_max_tokens: int | None) -> Any:
        return await llm_client.chat(
            messages,
            stream=True,
            on_stream_delta=on_token,
            response_format=response_format,
            max_tokens=effective_max_tokens,
            temperature=temperature,
            model_override=model_override,
            request_id=request_id,
        )

    if guard is None:
        call_count = current_call_count + 1
        result = await _call(max_tokens)
    else:
        result, call_count = await guard.invoke(
            current_call_count=current_call_count,
            request_id=request_id,
            model=model_override,
            max_tokens=max_tokens,
            call=_call,
        )
    call_metas = _physical_attempts(
        result,
        terminal_status=(
            "ok"
            if getattr(result.status, "value", result.status) == CallStatus.OK.value
            else "error"
        ),
        terminal_error=getattr(result, "error_text", None),
    )
    call_count += max(0, len(call_metas) - 1)
    if result.status != CallStatus.OK:
        logger.warning(
            "summarize_graph_streaming_failed",
            extra={"cid": correlation_id, "error": result.error_text},
        )
        err = ValueError(f"Streaming LLM call failed: {result.error_text}")
        # Attach the raw result so _tag_failure can build a fidelity record
        # (real model / error_text / latency without re-wrapping).
        err.__llm_result__ = result  # type: ignore[attr-defined]
        err.__llm_call_count__ = call_count  # type: ignore[attr-defined]
        err.__llm_physical_attempts__ = call_metas  # type: ignore[attr-defined]
        raise err

    # Provider adapters expose their full transport envelope in ``response_json``
    # (choices/usage for OpenAI/OpenRouter, content/usage for Anthropic). The actual
    # summary JSON is the assistant text, so parse that first. Test doubles and
    # future adapters may still return an already-unwrapped summary dict.
    parsed: Any = None
    parse_error: Exception | None = None
    if result.response_text:
        try:
            parsed = extract_json(result.response_text)
        except Exception as exc:
            parse_error = exc
    if not isinstance(parsed, dict) and _is_unwrapped_summary_json(result.response_json):
        parsed = result.response_json
    if not isinstance(parsed, dict):
        parsed = {
            "__stream_parse_error__": (
                type(parse_error).__name__ if parse_error is not None else "no_json_object"
            ),
            "__raw_stream_response__": (result.response_text or "")[
                :_STREAM_REPAIR_CANDIDATE_MAX_CHARS
            ],
        }

    summary = mark_prompt_injection_metadata(parsed, source_content)
    quality = summary.get("quality")
    merge_summary_quality_metadata(
        summary,
        model_used=result.model,
        structured_output_mode=structured_output_mode,
        prompt_injection_suspected=(
            quality.get("prompt_injection_suspected", False) if isinstance(quality, dict) else False
        ),
    )
    return summary, call_metas, call_count


def _physical_attempts(
    result: Any,
    *,
    terminal_status: str,
    terminal_error: str | None = None,
) -> list[dict[str, Any]]:
    """Normalize adapter telemetry to one serializable row per provider request."""
    structured = getattr(result, "physical_attempts", None)
    if isinstance(structured, list) and structured:
        return [dict(attempt) for attempt in structured if isinstance(attempt, dict)]

    attempts: list[dict[str, Any]] = []
    per_model = getattr(result, "per_model_attempts", None)
    if isinstance(per_model, list):
        attempts.extend(dict(attempt) for attempt in per_model if isinstance(attempt, dict))
    attempts.append(
        {
            "model": getattr(result, "model", None) or getattr(result, "model_used", None),
            "status": terminal_status,
            "tokens_prompt": getattr(result, "tokens_prompt", None),
            "tokens_completion": getattr(result, "tokens_completion", None),
            "cost_usd": getattr(result, "cost_usd", None),
            "latency_ms": getattr(result, "latency_ms", None),
            "error_text": terminal_error,
        }
    )
    return attempts


def _exception_physical_attempts(exc: Exception) -> list[dict[str, Any]]:
    raw = getattr(exc, "__llm_physical_attempts__", None)
    if isinstance(raw, list) and raw:
        return [dict(attempt) for attempt in raw if isinstance(attempt, dict)]
    result = getattr(exc, "__llm_result__", None)
    if result is not None:
        return _physical_attempts(
            result,
            terminal_status="error",
            terminal_error=getattr(result, "error_text", None) or str(exc),
        )
    return []


def _is_unwrapped_summary_json(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    provider_envelope_keys = {"choices", "usage", "content", "object", "id"}
    return not provider_envelope_keys.intersection(value)


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
    enrichment_content_max_chars: int = 30000,
    correlation_id: str | None = None,
    request_id: int | None = None,
    guard: GraphLLMGuard | None = None,
    current_call_count: int = 0,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Optional second enrichment pass (verbatim parity with ``enrich_two_pass``).

    Never raises: any failure (LLM error, parse failure, exception) returns the
    input ``summary`` unchanged. Merges only the 8 enrichment keys, and only when
    truthy.

    GAP 3b: returns ``(summary, call_meta | None)`` so the ``enrich`` node can
    record the enrichment LLM call in ``state['llm_calls']`` for persist-everything
    (rule 3). ``call_meta`` is ``None`` when no LLM call was made (exception path).
    """
    try:
        lang_suffix = "ru" if chosen_lang == LANG_RU else "en"
        enrichment_prompt = read_prompt_text(_PROMPTS_DIR / f"enrichment_system_{lang_suffix}.txt")
        core_summary_text = json_dumps(
            {k: v for k, v in summary.items() if k in _ENRICH_CORE_FIELDS}, indent=2
        )
        from app.core.content_cleaner import maybe_redact_pii, wrap_untrusted_source

        user_content = (
            f"Respond in {'Russian' if chosen_lang == LANG_RU else 'English'}.\n\n"
            f"CORE SUMMARY (already generated, do not modify):\n{core_summary_text}\n\n"
            + wrap_untrusted_source(maybe_redact_pii(content_text[:enrichment_content_max_chars]))
        )
        messages = [
            {"role": "system", "content": enrichment_prompt},
            {"role": "user", "content": user_content},
        ]

        async def _call(effective_max_tokens: int | None) -> Any:
            return await llm_client.chat(
                messages,
                response_format={"type": "json_object"},
                max_tokens=effective_max_tokens,
                temperature=temperature,
                top_p=top_p,
                request_id=request_id,
            )

        if guard is None:
            call_count = current_call_count + 1
            llm_result = await _call(enrichment_max_tokens)
        else:
            llm_result, call_count = await guard.invoke(
                current_call_count=current_call_count,
                request_id=request_id,
                model=None,
                max_tokens=enrichment_max_tokens,
                call=_call,
            )
        llm_status = getattr(llm_result, "status", None)
        status_value = getattr(llm_status, "value", llm_status)
        call_metas = _physical_attempts(
            llm_result,
            terminal_status="ok" if status_value == CallStatus.OK.value else "error",
            terminal_error=getattr(llm_result, "error_text", None),
        )
        call_count += max(0, len(call_metas) - 1)
        if llm_result.status != CallStatus.OK:
            logger.warning(
                "two_pass_enrichment_failed",
                extra={"cid": correlation_id, "error": getattr(llm_result, "error_text", None)},
            )
            return summary, call_metas, call_count
        enrichment = _parse_enrichment(llm_result)
        if not enrichment:
            logger.warning("two_pass_enrichment_parse_failed", extra={"cid": correlation_id})
            call_metas[-1]["status"] = "error"
            call_metas[-1]["error_text"] = "enrichment response was not valid JSON"
            return summary, call_metas, call_count
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
        return summary, call_metas, call_count
    except Exception as exc:
        logger.warning(
            "two_pass_enrichment_error", extra={"cid": correlation_id, "error": str(exc)}
        )
        call_count = int(getattr(exc, "__llm_call_count__", current_call_count))
        call_metas = _exception_physical_attempts(exc)
        if not call_metas:
            call_metas = [
                {
                    "model": None,
                    "status": "error",
                    "tokens_prompt": None,
                    "tokens_completion": None,
                    "cost_usd": None,
                    "latency_ms": None,
                    "error_text": str(exc),
                }
            ]
        call_count += max(0, len(call_metas) - 1)
        return summary, call_metas, call_count


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
