"""Pure summarization service for non-interactive workflows."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.core.call_status import CallStatus
from app.core.content_cleaner import clean_content_for_llm
from app.core.json_utils import dumps as json_dumps, extract_json
from app.core.lang import LANG_RU
from app.core.logging_utils import get_logger
from app.core.summary_contract import validate_and_shape_summary
from app.core.summary_contract_impl.quality_metadata import merge_summary_quality_metadata
from app.core.token_utils import count_tokens
from app.prompts.file_cache import read_prompt_text

from .summary_request_factory import (
    build_summary_user_prompt,
    log_llm_content_validation,
    mark_prompt_injection_metadata,
)

if TYPE_CHECKING:
    from .summarization_models import EnsureSummaryPayloadRequest, PureSummaryRequest
    from .summarization_runtime import SummarizationRuntime

logger = get_logger(__name__)

# Output-token budget bounds. The minimum still comfortably fits the full summary
# contract (summary_250/summary_1000/tldr/key_ideas/...); short content no longer
# pays a flat 4096-token floor.
_MIN_OUTPUT_TOKENS = 1536
_MAX_OUTPUT_TOKENS = 12288


class PureSummaryService:
    """LLM summarization without Telegram message dependencies."""

    def __init__(self, *, runtime: SummarizationRuntime) -> None:
        self._runtime = runtime

    async def summarize(self, request: PureSummaryRequest) -> dict[str, Any]:
        """Generate a summary payload without Telegram-side notifications."""
        if not request.content_text or not request.content_text.strip():
            raise ValueError("Content text is empty or contains only whitespace")

        content_for_summary = request.content_text
        model_override = None
        routing_cfg = self._runtime.cfg.model_routing
        threshold_tokens = (
            routing_cfg.long_context_threshold_tokens if routing_cfg.enabled else 80000
        )
        approx_input_tokens = count_tokens(request.content_text)
        if approx_input_tokens > threshold_tokens:
            long_ctx_model = (
                routing_cfg.long_context_model
                if routing_cfg.enabled
                else self._runtime.cfg.openrouter.long_context_model
            )
            if long_ctx_model:
                model_override = long_ctx_model
            else:
                # Truncate by tokens using the content's real char/token ratio, so
                # CJK/Cyrillic text (fewer chars per token than English) is handled
                # correctly instead of slipping past a fixed character threshold and
                # overflowing the model context window.
                chars_per_token = len(request.content_text) / max(approx_input_tokens, 1)
                max_chars = max(1, int(threshold_tokens * chars_per_token))
                content_for_summary = self._truncate_content(
                    request.content_text,
                    max_chars,
                )
                logger.info(
                    "summarize_pure_truncated",
                    extra={
                        "cid": request.correlation_id,
                        "original_len": len(request.content_text),
                        "truncated_len": len(content_for_summary),
                        "approx_input_tokens": approx_input_tokens,
                        "threshold_tokens": threshold_tokens,
                        "max_chars": max_chars,
                    },
                )

        # Content-aware model routing (lower priority than long-context)
        if model_override is None and routing_cfg.enabled:
            from app.core.content_classifier import classify_content
            from app.core.model_router import resolve_model_for_content

            tier = classify_content(content_for_summary)
            model_override = resolve_model_for_content(
                tier=tier,
                content_length=len(content_for_summary),
                has_images=False,
                routing_config=routing_cfg,
                openrouter_config=self._runtime.cfg.openrouter,
            )

        content_for_summary = clean_content_for_llm(content_for_summary)
        user_content = build_summary_user_prompt(
            content_for_summary=content_for_summary,
            chosen_lang=request.chosen_lang,
            feedback_instructions=request.feedback_instructions,
        )

        log_llm_content_validation(
            cfg=self._runtime.cfg,
            content_text=content_for_summary,
            system_prompt=request.system_prompt,
            user_content=user_content,
            correlation_id=request.correlation_id,
        )

        system_prompt = self._load_instructor_prompt(request.chosen_lang)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        max_tokens = self.select_max_tokens(content_for_summary)

        logger.info(
            "summarize_pure_start",
            extra={
                "cid": request.correlation_id,
                "content_len": len(content_for_summary),
                "lang": request.chosen_lang,
                "has_feedback": bool(request.feedback_instructions),
                "model": model_override or self._runtime.cfg.openrouter.model,
            },
        )

        summary = await self._summarize_with_instructor(
            messages=messages,
            source_content=content_for_summary,
            max_tokens=max_tokens,
            model_override=model_override,
            correlation_id=request.correlation_id,
        )
        return self._apply_request_quality_metadata(
            summary,
            source_coverage=request.source_coverage,
            extraction_quality=request.extraction_quality,
            extraction_confidence=request.extraction_confidence,
        )

    @staticmethod
    def _classify_sticky_error(exc: Exception) -> str | None:
        """Return the sticky-class label if *exc* represents a sticky failure, else None.

        Sticky classes are the three error strings that chat_attempt_runner sets on
        ``state.last_error_text`` when a model should be abandoned rather than
        retried with the same override:

        - ``per_model_timeout``
        - ``repeated_truncation``
        - ``truncation_recovery_skipped_budget_tight``

        The exception text is inspected because that is what bubbles up through
        instructor/OpenAI to this layer.
        """
        text = str(exc)
        for label in (
            "per_model_timeout",
            "repeated_truncation",
            "truncation_recovery_skipped_budget_tight",
        ):
            if label in text:
                return label
        return None

    async def _summarize_with_instructor(
        self,
        messages: list[dict[str, Any]],
        *,
        source_content: str,
        max_tokens: int | None,
        model_override: str | None,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        from app.core.summary_schema import SummaryModel

        # Each retry re-runs the full model cascade for one summary, so a high
        # cap multiplies wall-clock cost when validation keeps failing. The
        # SUMMARIZATION_MAX_RETRIES env var lets operators dial this down on
        # constrained hosts (e.g. Pi) where 2 attempts is the right balance.
        max_retries = int(getattr(self._runtime.cfg.runtime, "summarization_max_retries", 3))
        sticky_fallback_enabled = bool(
            getattr(self._runtime.cfg.runtime, "llm_sticky_failure_force_fallback", True)
        )

        result = None
        last_error: Exception | None = None
        override_dropped = False

        for attempt in range(2):  # at most one retry
            current_override = None if override_dropped else model_override
            try:
                async with self._runtime.sem():
                    result = await self._runtime.openrouter.chat_structured(
                        messages,
                        response_model=SummaryModel,
                        max_retries=max_retries,
                        temperature=self._runtime.cfg.openrouter.temperature,
                        max_tokens=max_tokens,
                        model_override=current_override,
                    )
                break
            except Exception as exc:
                last_error = exc
                sticky_class = self._classify_sticky_error(exc)
                # Retry once: only on sticky errors, only when the flag is on,
                # only when there is an override to drop, and only on the first attempt.
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
                    "summarize_pure_instructor_failed",
                    extra={"cid": correlation_id, "error": str(exc)},
                )
                raise ValueError(f"Instructor LLM call failed: {exc}") from exc

        if result is None:
            logger.error(
                "summarize_pure_instructor_failed",
                extra={"cid": correlation_id, "error": str(last_error)},
            )
            raise ValueError(f"Instructor LLM call failed: {last_error}") from last_error

        summary = mark_prompt_injection_metadata(result.parsed.model_dump(), source_content)
        merge_summary_quality_metadata(
            summary,
            model_used=result.model_used,
            structured_output_mode=self._runtime.cfg.openrouter.structured_output_mode,
            prompt_injection_suspected=summary.get("quality", {}).get(
                "prompt_injection_suspected", False
            )
            if isinstance(summary.get("quality"), dict)
            else False,
        )
        logger.info(
            "summarize_pure_success",
            extra={
                "cid": correlation_id,
                "summary_keys": list(summary.keys()),
                "model": result.model_used,
                "tokens_prompt": result.tokens_prompt,
                "tokens_completion": result.tokens_completion,
                "instructor": True,
            },
        )
        return summary

    @staticmethod
    def _load_instructor_prompt(lang: str) -> str:
        from app.core.lang import LANG_RU

        lang_suffix = "ru" if lang == LANG_RU else "en"
        prompt_path = (
            Path(__file__).resolve().parent.parent.parent
            / "prompts"
            / f"summary_system_{lang_suffix}_instructor.txt"
        )
        return read_prompt_text(prompt_path)

    async def ensure_summary_payload(self, request: EnsureSummaryPayloadRequest) -> dict[str, Any]:
        """Validate and enrich a parsed summary payload."""
        if not isinstance(request.summary, dict):
            raise ValueError("Summary payload must be a dictionary")

        shaped = validate_and_shape_summary(request.summary)
        shaped = await self._runtime.metadata_helper.ensure_summary_metadata(
            shaped,
            request.req_id,
            request.content_text,
            request.correlation_id,
            request.chosen_lang,
        )
        shaped = mark_prompt_injection_metadata(shaped, request.content_text)
        self._apply_request_quality_metadata(
            shaped,
            source_coverage=request.source_coverage,
            extraction_quality=request.extraction_quality,
            extraction_confidence=request.extraction_confidence,
            prompt_injection_suspected=shaped.get("quality", {}).get(
                "prompt_injection_suspected", False
            )
            if isinstance(shaped.get("quality"), dict)
            else False,
        )
        self._runtime.insights_generator.update_last_summary(shaped)
        return shaped

    @staticmethod
    def _apply_request_quality_metadata(
        summary: dict[str, Any],
        *,
        source_coverage: str | None,
        extraction_quality: str | None,
        extraction_confidence: float | None,
        prompt_injection_suspected: bool | None = None,
    ) -> dict[str, Any]:
        merge_summary_quality_metadata(
            summary,
            source_coverage=source_coverage,
            extraction_quality=extraction_quality,
            extraction_confidence=extraction_confidence,
            prompt_injection_suspected=prompt_injection_suspected,
        )
        return summary

    async def enrich_two_pass(
        self,
        summary: dict[str, Any],
        *,
        content_text: str,
        chosen_lang: str,
        correlation_id: str | None,
    ) -> dict[str, Any]:
        """Run the optional second-pass enrichment flow."""
        try:
            prompt_dir = Path(__file__).resolve().parent.parent.parent / "prompts"
            lang_suffix = "ru" if chosen_lang == LANG_RU else "en"
            prompt_path = prompt_dir / f"enrichment_system_{lang_suffix}.txt"
            enrichment_prompt = read_prompt_text(prompt_path)

            core_fields = {
                "summary_250",
                "summary_1000",
                "tldr",
                "key_ideas",
                "topic_tags",
                "entities",
                "source_type",
            }
            core_summary_text = json_dumps(
                {key: value for key, value in summary.items() if key in core_fields},
                indent=2,
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

            async with self._runtime.sem():
                llm_result = await self._runtime.openrouter.chat(
                    messages,
                    response_format=self._runtime.workflow.build_structured_response_format(
                        mode="json_object"
                    ),
                    max_tokens=4096,
                    temperature=self._runtime.cfg.openrouter.temperature,
                    top_p=self._runtime.cfg.openrouter.top_p,
                    request_id=None,
                )

            if llm_result.status != CallStatus.OK:
                logger.warning(
                    "two_pass_enrichment_failed",
                    extra={"cid": correlation_id, "error": llm_result.error_text},
                )
                return summary

            enrichment = self.parse_summary_from_llm_result(llm_result)
            if not enrichment:
                logger.warning(
                    "two_pass_enrichment_parse_failed",
                    extra={"cid": correlation_id},
                )
                return summary

            enrichment_keys = {
                "answered_questions",
                "seo_keywords",
                "extractive_quotes",
                "highlights",
                "categories",
                "key_points_to_remember",
                "questions_answered",
                "topic_taxonomy",
            }
            for key in enrichment_keys:
                value = enrichment.get(key)
                if value:
                    summary[key] = value

            logger.info(
                "two_pass_enrichment_merged",
                extra={
                    "cid": correlation_id,
                    "enriched_fields": [key for key in enrichment_keys if key in enrichment],
                },
            )
            return summary
        except Exception as exc:
            logger.warning(
                "two_pass_enrichment_error",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            return summary

    def parse_summary_from_llm_result(self, llm_result: Any) -> dict[str, Any] | None:
        """Parse a summary payload from an LLM result object."""
        if isinstance(llm_result.response_json, dict):
            choices = llm_result.response_json.get("choices") or []
            if choices:
                message = (choices[0] or {}).get("message") or {}
                parsed = message.get("parsed")
                if isinstance(parsed, dict):
                    return parsed
                content = message.get("content")
                if isinstance(content, str):
                    extracted = extract_json(content)
                    if isinstance(extracted, dict):
                        return extracted

        if llm_result.response_text:
            extracted = extract_json(llm_result.response_text)
            if isinstance(extracted, dict):
                return extracted
        return None

    def select_max_tokens(self, content_text: str) -> int | None:
        """Choose a cost-aware output token budget."""
        configured = self._runtime.cfg.openrouter.max_tokens
        approx_input_tokens = count_tokens(content_text)
        # Output scales with input; small inputs get a proportionally smaller budget.
        dynamic_budget = max(
            _MIN_OUTPUT_TOKENS, min(_MAX_OUTPUT_TOKENS, approx_input_tokens // 2 + 1024)
        )

        if configured is None:
            logger.debug(
                "max_tokens_dynamic",
                extra={
                    "content_len": len(content_text),
                    "approx_input_tokens": approx_input_tokens,
                    "selected": dynamic_budget,
                },
            )
            return dynamic_budget

        selected = max(_MIN_OUTPUT_TOKENS, min(configured, dynamic_budget))
        logger.debug(
            "max_tokens_adjusted",
            extra={
                "content_len": len(content_text),
                "approx_input_tokens": approx_input_tokens,
                "configured": configured,
                "selected": selected,
            },
        )
        return selected

    @staticmethod
    def _truncate_content(content_text: str, max_chars: int) -> str:
        from app.adapters.content.llm_summarizer_text import truncate_content_text

        return truncate_content_text(content_text, max_chars)
