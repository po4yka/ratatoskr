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
from app.core.token_utils import count_tokens

from .summary_request_factory import (
    build_summary_user_prompt,
    log_llm_content_validation,
    mark_prompt_injection_metadata,
)

if TYPE_CHECKING:
    from .summarization_models import EnsureSummaryPayloadRequest, PureSummaryRequest
    from .summarization_runtime import SummarizationRuntime

logger = get_logger(__name__)


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
        max_chars_threshold = (
            (routing_cfg.long_context_threshold_tokens * 4) if routing_cfg.enabled else 320000
        )
        if len(request.content_text) > max_chars_threshold:
            long_ctx_model = (
                routing_cfg.long_context_model
                if routing_cfg.enabled
                else self._runtime.cfg.openrouter.long_context_model
            )
            if long_ctx_model:
                model_override = long_ctx_model
            else:
                content_for_summary = self._truncate_content(
                    request.content_text,
                    max_chars_threshold,
                )
                logger.info(
                    "summarize_pure_truncated",
                    extra={
                        "cid": request.correlation_id,
                        "original_len": len(request.content_text),
                        "truncated_len": len(content_for_summary),
                        "max_chars": max_chars_threshold,
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

        return await self._summarize_with_instructor(
            messages=messages,
            source_content=content_for_summary,
            max_tokens=max_tokens,
            model_override=model_override,
            correlation_id=request.correlation_id,
        )

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

        try:
            async with self._runtime.sem():
                result = await self._runtime.openrouter.chat_structured(
                    messages,
                    response_model=SummaryModel,
                    max_retries=3,
                    temperature=self._runtime.cfg.openrouter.temperature,
                    max_tokens=max_tokens,
                    model_override=model_override,
                )
        except Exception as exc:
            logger.error(
                "summarize_pure_instructor_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            raise ValueError(f"Instructor LLM call failed: {exc}") from exc

        summary = mark_prompt_injection_metadata(result.parsed.model_dump(), source_content)
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
        return prompt_path.read_text(encoding="utf-8")

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
        self._runtime.insights_generator.update_last_summary(shaped)
        return shaped

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
            enrichment_prompt = prompt_path.read_text(encoding="utf-8")

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
        dynamic_budget = max(4096, min(12288, approx_input_tokens // 2 + 2048))

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

        selected = max(4096, min(configured, dynamic_budget))
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
