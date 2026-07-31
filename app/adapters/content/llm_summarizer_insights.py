"""Insights generation helper for LLM summarization."""

from __future__ import annotations

import json
from typing import Any, cast

from app.core.async_utils import raise_if_cancelled
from app.core.call_status import CallStatus
from app.core.json_utils import extract_json
from app.core.lang import LANG_RU
from app.core.logging_utils import get_logger

logger = get_logger(__name__)


def insights_has_content(payload: dict[str, Any]) -> bool:
    """Return True when the insights payload contains meaningful data."""
    for field in ("topic_overview", "caution"):
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return True

    list_fields = (
        "new_facts",
        "open_questions",
        "suggested_sources",
        "expansion_topics",
        "next_exploration",
    )
    for field in list_fields:
        items = payload.get(field)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, str) and item.strip():
                return True
            if isinstance(item, dict):
                for value in item.values():
                    if isinstance(value, str) and value.strip():
                        return True
                    if value not in (None, "", [], {}):
                        return True
            elif item not in (None, "", [], {}):
                return True

    return False


class LLMInsightsGenerator:
    """Generate and cache additional researched insights."""

    def __init__(
        self,
        *,
        cfg: Any,
        openrouter: Any,
        workflow: Any,
        summary_repo: Any,
        cache_helper: Any,
        sem: Any,
        coerce_string_list: Any,
        truncate_content_text: Any,
    ) -> None:
        self._cfg = cfg
        self._openrouter = openrouter
        self._workflow = workflow
        self._summary_repo = summary_repo
        self._cache_helper = cache_helper
        self._sem = sem
        self._coerce_string_list = coerce_string_list
        self._truncate_content_text = truncate_content_text

    def has_content(self, payload: dict[str, Any]) -> bool:
        return insights_has_content(payload)

    def select_max_tokens(self, content_text: str) -> int | None:
        """Choose an appropriate max_tokens budget for insights generation."""
        configured = self._cfg.openrouter.max_tokens

        # Insights typically need more tokens than summaries for detailed analysis
        approx_input_tokens = max(1, len(content_text) // 4)
        # Much higher budget for insights: comprehensive facts, analysis, and research details
        dynamic_budget = max(3072, min(12288, approx_input_tokens // 2 + 3072))

        if configured is None:
            logger.debug(
                "insights_max_tokens_dynamic",
                extra={
                    "content_len": len(content_text),
                    "approx_input_tokens": approx_input_tokens,
                    "selected": dynamic_budget,
                },
            )
            return dynamic_budget

        # Use much higher minimum for insights than regular summaries
        selected = max(3072, min(configured, dynamic_budget))

        logger.debug(
            "insights_max_tokens_adjusted",
            extra={
                "content_len": len(content_text),
                "approx_input_tokens": approx_input_tokens,
                "configured": configured,
                "dynamic": dynamic_budget,
                "selected": selected,
            },
        )
        return cast("int", selected)

    async def generate_additional_insights(
        self,
        message: Any,
        *,
        content_text: str,
        chosen_lang: str,
        req_id: int,
        correlation_id: str | None,
        summary: dict[str, Any] | None = None,
        url_hash: str | None = None,
    ) -> dict[str, Any] | None:
        """Call OpenRouter to obtain additional researched insights for the article."""
        if not content_text.strip():
            return None

        summary_candidate = await self._resolve_summary_candidate(
            req_id=req_id,
            summary=summary,
            correlation_id=correlation_id,
        )
        reused_insights = self._reuse_summary_insights(
            summary_candidate=summary_candidate,
            req_id=req_id,
            correlation_id=correlation_id,
        )
        if reused_insights is not None:
            return reused_insights

        candidate_models: list[str] = [self._cfg.openrouter.model]
        candidate_models.extend(
            [
                model
                for model in self._cfg.openrouter.fallback_models
                if model and model not in candidate_models
            ]
        )

        if url_hash:
            for model_name in candidate_models:
                cached = await self._cache_helper.get_cached_insights(
                    url_hash, chosen_lang, model_name, correlation_id
                )
                if cached:
                    logger.info(
                        "insights_cache_hit",
                        extra={"cid": correlation_id, "model": model_name},
                    )
                    if isinstance(summary_candidate, dict):
                        summary_candidate.setdefault("insights", cached)
                    return cast("dict[str, Any]", cached)

        system_prompt = self._build_insights_system_prompt(chosen_lang)
        source_text = self._build_insights_source_text(content_text, summary_candidate)
        content_for_insights = self._truncate_insights_text(source_text)
        user_prompt = self._build_insights_user_prompt(content_for_insights, chosen_lang)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        try:
            response_formats: list[dict[str, Any]] = []
            primary_format = self._build_insights_response_format()
            response_formats.append(primary_format)
            if primary_format.get("type") != "json_object":
                response_formats.append({"type": "json_object"})

            for model_name in candidate_models:
                for response_format in response_formats:
                    async with self._sem():
                        llm = await self._openrouter.chat(
                            messages,
                            temperature=self._cfg.openrouter.temperature,
                            max_tokens=self.select_max_tokens(content_for_insights),
                            top_p=self._cfg.openrouter.top_p,
                            request_id=req_id,
                            response_format=response_format,
                            model_override=model_name,
                        )

                    await self._workflow.persist_llm_call(llm, req_id, correlation_id)

                    if llm.status != CallStatus.OK:
                        structured_error = (llm.error_text or "") == "structured_output_parse_error"
                        logger.warning(
                            "insights_llm_error",
                            extra={
                                "cid": correlation_id,
                                "status": llm.status,
                                "error": llm.error_text,
                                "model": model_name,
                                "response_format": response_format.get("type"),
                            },
                        )
                        if structured_error:
                            # Try the next response format or model
                            continue
                        return None

                    insights = self._parse_insights_response(llm.response_json, llm.response_text)
                    if not insights:
                        logger.warning(
                            "insights_parse_failed",
                            extra={
                                "cid": correlation_id,
                                "model": model_name,
                                "response_format": response_format.get("type"),
                            },
                        )
                        # Try next combination
                        continue

                    logger.info(
                        "insights_generation_success",
                        extra={
                            "cid": correlation_id,
                            "model": model_name,
                            "response_format": response_format.get("type"),
                            "facts_count": len(insights.get("new_facts", []) or []),
                        },
                    )
                    if url_hash:
                        await self._cache_helper.write_insights_cache(
                            url_hash, model_name, chosen_lang, insights
                        )
                    return insights

            return None

        except Exception as exc:
            raise_if_cancelled(exc)
            logger.exception(
                "insights_generation_failed", extra={"cid": correlation_id, "error": str(exc)}
            )
            return None

    async def _resolve_summary_candidate(
        self,
        *,
        req_id: int,
        summary: dict[str, Any] | None,
        correlation_id: str | None,
    ) -> dict[str, Any] | None:
        """Return the best summary payload candidate for insights generation."""
        # Was `summary or self._last_summary_shaped`. That cross-request cache was
        # only ever reset by reset_state(), which nothing called, so a miss fell
        # back to whatever the previous request left behind. The per-req_id DB load
        # below is the correct source.
        summary_candidate = summary
        if summary_candidate is not None:
            return summary_candidate if isinstance(summary_candidate, dict) else None

        try:
            row = await self._summary_repo.async_get_summary_by_request(req_id)
            json_payload = row.get("json_payload") if row else None
            if not json_payload:
                return None
            return json_payload if isinstance(json_payload, dict) else json.loads(json_payload)
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.debug(
                "insights_summary_load_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            return None

    def _reuse_summary_insights(
        self,
        *,
        summary_candidate: dict[str, Any] | None,
        req_id: int,
        correlation_id: str | None,
    ) -> dict[str, Any] | None:
        """Return cached insights embedded in summary payload when available."""
        if not isinstance(summary_candidate, dict):
            return None
        insights_payload = summary_candidate.get("insights")
        if not (isinstance(insights_payload, dict) and insights_has_content(insights_payload)):
            return None

        logger.info(
            "insights_reused_from_summary",
            extra={"cid": correlation_id, "request_id": req_id, "source": "summary_payload"},
        )
        return insights_payload

    def _build_insights_source_text(self, content_text: str, summary: dict[str, Any] | None) -> str:
        parts: list[str] = []
        if isinstance(summary, dict):
            tldr = summary.get("tldr")
            if isinstance(tldr, str) and tldr.strip():
                parts.append(f"TLDR:\n{tldr.strip()}")

            summary_1000 = summary.get("summary_1000")
            if isinstance(summary_1000, str) and summary_1000.strip():
                parts.append(f"SUMMARY:\n{summary_1000.strip()}")

            summary_250 = summary.get("summary_250")
            if isinstance(summary_250, str) and summary_250.strip():
                parts.append(f"SUMMARY_250:\n{summary_250.strip()}")

            key_ideas = self._coerce_string_list(summary.get("key_ideas"))
            if key_ideas:
                parts.append("KEY IDEAS:\n- " + "\n- ".join(key_ideas[:8]))

            topic_tags = self._coerce_string_list(summary.get("topic_tags"))
            if topic_tags:
                parts.append("TOPIC TAGS:\n" + ", ".join(topic_tags[:12]))

            key_stats = self._coerce_string_list(summary.get("key_stats"))
            if key_stats:
                parts.append("KEY STATS:\n- " + "\n- ".join(key_stats[:6]))

        assembled = "\n\n".join(parts).strip()
        return assembled or content_text

    def _truncate_insights_text(self, content_text: str, max_chars: int = 12000) -> str:
        if len(content_text) <= max_chars:
            return content_text
        truncated = self._truncate_content_text(content_text, max_chars)
        logger.info(
            "insights_source_truncated",
            extra={"original_len": len(content_text), "truncated_len": len(truncated)},
        )
        return cast("str", truncated)

    def _build_insights_system_prompt(self, lang: str) -> str:
        """Return system prompt instructing additional insight behaviour."""
        if lang == LANG_RU:
            return (
                "Ты аналитик-исследователь. Используя статью и проверенные знания,"
                " дай свежие факты, контекст и вопросы по теме. Отмечай низкую уверенность"
                " и строго соблюдай требуемую JSON-схему."
                " Добавь разделы: 'expansion_topics' (новые направления/темы для обсуждения,"
                " не основанные напрямую на тексте) и 'next_exploration' (что изучить далее:"
                " гипотезы, эксперименты, источники и метрики)."
            )
        return (
            "You are an investigative research analyst. Combine the article with your tested"
            " knowledge to surface fresh facts, context, recent developments, and open"
            " questions. Flag any low-confidence items and answer using the JSON schema."
            " Include sections: 'expansion_topics' (new, beyond-text themes worth exploring)"
            " and 'next_exploration' (what to explore next: hypotheses, experiments, sources,"
            " and metrics)."
        )

    def _build_insights_user_prompt(self, content_text: str, lang: str) -> str:
        """Return user prompt used for additional insights."""
        lang_label = "Russian" if lang == LANG_RU else "English"
        return (
            "Provide concise research insights that extend beyond the literal article text."
            " Include relevant historical context, recent developments (up to your knowledge"
            " cut-off), market or technical implications, and unanswered questions."
            " Mark any uncertain statements as low confidence."
            f" Respond in {lang_label}."
            "\n\nReturn strictly valid JSON with the exact structure below (include every key even if empty):"
            "\n{"
            '\n  "topic_overview": string,'
            '\n  "new_facts": ['
            '\n    {\n      "fact": string,\n      "why_it_matters": string | null,\n      "source_hint": string | null,\n      "confidence": number | string | null\n    }'
            "\n  ],"
            '\n  "open_questions": [string],'
            '\n  "suggested_sources": [string],'
            '\n  "expansion_topics": [string],'
            '\n  "next_exploration": [string],'
            '\n  "caution": string | null'
            "\n}"
            "\nAim for 4-6 items per list when possible; use empty arrays when information is unavailable."
            "\n\nARTICLE CONTENT START\n"
            f"{content_text}\n"
            "ARTICLE CONTENT END"
        )

    def _build_insights_response_format(self) -> dict[str, Any]:
        """Build response format configuration for insights request."""
        return {
            "type": "json_schema",
            "json_schema": {
                "name": "insights_schema",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "topic_overview": {"type": "string"},
                        "new_facts": {
                            "type": "array",
                            "minItems": 0,
                            "maxItems": 5,
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "fact": {"type": "string"},
                                    "why_it_matters": {"type": ["string", "null"]},
                                    "source_hint": {"type": ["string", "null"]},
                                    "confidence": {"type": ["number", "string", "null"]},
                                },
                                "required": ["fact"],
                            },
                        },
                        "open_questions": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "suggested_sources": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "caution": {"type": ["string", "null"]},
                        "expansion_topics": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "next_exploration": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["topic_overview"],
                },
            },
        }

    def _parse_insights_response(
        self, response_json: Any, response_text: str | None
    ) -> dict[str, Any] | None:
        """Parse structured insights payload from LLM output."""
        candidate: dict[str, Any] | None = None

        if isinstance(response_json, dict):
            choices = response_json.get("choices")
            if isinstance(choices, list) and choices:
                first = choices[0] or {}
                if isinstance(first, dict):
                    message = first.get("message") or {}
                    if isinstance(message, dict):
                        parsed = message.get("parsed")
                        if isinstance(parsed, dict):
                            candidate = parsed
                        elif parsed is not None:
                            try:
                                loaded = json.loads(json.dumps(parsed))
                                if isinstance(loaded, dict):
                                    candidate = loaded
                            except Exception:
                                candidate = None
                        if candidate is None:
                            content = message.get("content")
                            if isinstance(content, str):
                                candidate = extract_json(content) or None

        if candidate is None and response_text:
            textracted = extract_json(response_text)
            if isinstance(textracted, dict):
                candidate = textracted

        if not isinstance(candidate, dict):
            return None

        # Normalize and clean new_facts list (if present)
        facts = candidate.get("new_facts")
        if isinstance(facts, list):
            cleaned: list[dict[str, Any]] = []
            for fact in facts:
                if not isinstance(fact, dict):
                    continue
                fact_text = str(fact.get("fact", "")).strip()
                if not fact_text:
                    continue
                cleaned.append(fact)
            candidate["new_facts"] = cleaned

        # Return the candidate even if new_facts is empty; the formatter will
        # still send a graceful message and this ensures the follow-up message
        # is not silently suppressed.
        return candidate
