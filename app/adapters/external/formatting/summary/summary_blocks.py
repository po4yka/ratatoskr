"""Reusable field and supplemental block rendering for summaries."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.core.ui_strings import t

if TYPE_CHECKING:
    from .presenter_context import SummaryPresenterContext

logger = get_logger(__name__)


class SummaryBlocksPresenter:
    """Build and send summary field blocks."""

    def __init__(self, context: SummaryPresenterContext) -> None:
        self._context = context

    @staticmethod
    def _trim_trailing_blank_lines(lines: list[str]) -> list[str]:
        while lines and not lines[-1]:
            lines.pop()
        return lines

    @staticmethod
    def _clean_string_list(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return [str(v).strip() for v in values if str(v).strip()]

    @staticmethod
    def _summarize_visible_items(items: list[str], limit: int, *, joiner: str) -> str:
        shown = items[:limit]
        hidden = max(0, len(items) - len(shown))
        tail = f" (+{hidden})" if hidden else ""
        return joiner.join(shown) + tail

    def build_combined_summary_lines(
        self, shaped: dict[str, Any], *, include_domain: bool
    ) -> list[str]:
        _l = self._context.lang
        combined_lines: list[str] = []

        tl_dr = str(shaped.get("summary_250", "")).strip()
        if tl_dr:
            tl_dr_clean = self._context.text_processor.sanitize_summary_text(tl_dr)
            combined_lines.extend([f"📋 {t('tldr', _l)}:", tl_dr_clean, ""])

        tag_items = self._clean_string_list(shaped.get("topic_tags") or [])
        if tag_items:
            combined_lines.append(
                f"🏷️ {t('tags', _l)}: " + self._summarize_visible_items(tag_items, 5, joiner=" ")
            )
            combined_lines.append("")

        entities = shaped.get("entities") or {}
        if isinstance(entities, dict):
            people = self._clean_string_list(entities.get("people") or [])
            orgs = self._clean_string_list(entities.get("organizations") or [])
            locs = self._clean_string_list(entities.get("locations") or [])
            if people or orgs or locs:
                combined_lines.append(f"🧭 {t('entities', _l)}:")
                if people:
                    combined_lines.append(
                        f"• {t('people', _l)}: "
                        + self._summarize_visible_items(people, 5, joiner=", ")
                    )
                if orgs:
                    combined_lines.append(
                        f"• {t('orgs', _l)}: " + self._summarize_visible_items(orgs, 5, joiner=", ")
                    )
                if locs:
                    combined_lines.append(
                        f"• {t('places', _l)}: "
                        + self._summarize_visible_items(locs, 5, joiner=", ")
                    )
                combined_lines.append("")

        reading_time = shaped.get("estimated_reading_time_min")
        if reading_time:
            combined_lines.append(f"⏱️ {t('reading_time', _l)}: ~{reading_time} min")
            combined_lines.append("")

        key_stats = shaped.get("key_stats") or []
        if isinstance(key_stats, list) and key_stats:
            ks_lines = self._context.data_formatter.format_key_stats(key_stats[:10])
            if ks_lines:
                combined_lines.append(f"📈 {t('key_stats', _l)}:")
                combined_lines.extend(ks_lines)
                combined_lines.append("")

        readability = shaped.get("readability") or {}
        readability_line = self._context.data_formatter.format_readability(readability)
        if readability_line:
            combined_lines.append(f"🧮 {t('readability', _l)} — {readability_line}")
            combined_lines.append("")

        metadata = shaped.get("metadata") or {}
        if isinstance(metadata, dict):
            meta_parts = []
            if metadata.get("title"):
                meta_parts.append(f"📰 {metadata['title']}")
            if metadata.get("author"):
                meta_parts.append(f"✍️ {metadata['author']}")
            if include_domain and metadata.get("domain"):
                meta_parts.append(f"🌐 {metadata['domain']}")
            if meta_parts:
                combined_lines.extend(meta_parts)
                combined_lines.append("")

        categories = self._clean_string_list(shaped.get("categories") or [])
        if categories:
            combined_lines.append(f"📁 {t('categories', _l)}: " + ", ".join(categories[:10]))
            combined_lines.append("")

        confidence = shaped.get("confidence", 0.0)
        low_confidence = isinstance(confidence, (int, float)) and confidence < 1.0
        risk = str(shaped.get("hallucination_risk", "unknown"))
        if low_confidence:
            combined_lines.append(f"🎯 {t('confidence', _l)}: {confidence:.1%}")
        if risk != "low":
            risk_emoji = "🚨" if risk == "high" else "⚠️"
            combined_lines.append(f"{risk_emoji} {t('hallucination_risk', _l)}: {risk}")
        if low_confidence or risk != "low":
            combined_lines.append("")

        return self._trim_trailing_blank_lines(combined_lines)

    @staticmethod
    def _summary_sort_key(field_name: str) -> int:
        if field_name == "tldr":
            return 10_000
        try:
            return int(field_name.split("_", 1)[1])
        except Exception:
            logger.debug("sort_key_extraction_failed", exc_info=True)
            return 0

    def summary_field_keys(self, shaped: dict[str, Any], *, include_tldr: bool) -> list[str]:
        fields = [
            key
            for key in shaped
            if (key.startswith("summary_") and key.split("_", 1)[1].isdigit())
            or (include_tldr and key == "tldr")
        ]
        return sorted(fields, key=self._summary_sort_key)

    def build_summary_field_text(self, shaped: dict[str, Any], *, include_tldr: bool) -> str | None:
        """Return formatted text for the longest summary field, or None."""
        _l = self._context.lang
        fields = self.summary_field_keys(shaped, include_tldr=include_tldr)
        if not fields:
            return None
        key = fields[-1]
        content = str(shaped.get(key, "")).strip()
        if not content:
            return None
        content = self._context.text_processor.sanitize_summary_text(content)
        label = (
            f"🧾 {t('tldr', _l)}"
            if key == "tldr"
            else f"🧾 {t('summary_n', _l)} {key.split('_', 1)[1]}"
        )
        return f"{label}:\n{content}"

    def build_key_ideas_text(self, shaped: dict[str, Any]) -> str | None:
        """Return formatted key ideas text, or None."""
        ideas = self._clean_string_list(shaped.get("key_ideas") or [])
        if not ideas:
            return None
        bullets = "\n".join(f"• {idea}" for idea in ideas)
        return f"<b>💡 {t('key_ideas', self._context.lang)}</b>\n{bullets}"

    def build_all_supplemental_blocks(self, shaped: dict[str, Any]) -> str | None:
        """Build ALL supplemental blocks as a single concatenated string.

        Coalesces: extractive quotes, highlights, questions answered,
        key points, perspective quality, taxonomy, forward extras, insights.
        """
        html_blocks = [
            self._build_extractive_quotes_message(shaped),
            self._build_bullet_message(
                f"<b>✨ {t('highlights', self._context.lang)}</b>",
                self._clean_string_list(shaped.get("highlights") or []),
                limit=10,
            ),
            self._build_questions_answered_message(shaped),
            self._build_bullet_message(
                f"<b>🎯 {t('key_points', self._context.lang)}</b>",
                self._clean_string_list(shaped.get("key_points_to_remember") or []),
                limit=10,
            ),
            self._build_quality_message(shaped),
            self._build_taxonomy_message(shaped),
            self._build_forward_extras_message(shaped),
        ]
        # Add insights blocks
        html_blocks.extend(self._build_insights_messages(shaped))

        # Filter None, join with double newline
        non_empty = [b for b in html_blocks if b]
        return "\n\n".join(non_empty) if non_empty else None

    def _build_extractive_quotes_message(self, shaped: dict[str, Any]) -> str | None:
        quotes = shaped.get("extractive_quotes") or []
        if not isinstance(quotes, list) or not quotes:
            return None
        lines = [f"<b>💬 {t('key_quotes', self._context.lang)}</b>"]
        for i, quote in enumerate(quotes[:5], 1):
            if not isinstance(quote, dict) or not quote.get("text"):
                continue
            text = str(quote["text"]).strip()
            if text:
                lines.append(f"<blockquote>{i}. {html.escape(text)}</blockquote>")
        return "\n".join(lines) if len(lines) > 1 else None

    @staticmethod
    def _build_bullet_message(
        title: str, values: list[str], *, limit: int, escape: bool = False
    ) -> str | None:
        if not values:
            return None
        items = values[:limit]
        if escape:
            items = [html.escape(item) for item in items]
        return title + "\n" + "\n".join(f"• {item}" for item in items)

    def _build_questions_answered_message(self, shaped: dict[str, Any]) -> str | None:
        questions_answered = shaped.get("questions_answered") or []
        if not isinstance(questions_answered, list) or not questions_answered:
            return None

        qa_lines = [f"<b>❓ {t('questions_answered', self._context.lang)}</b>"]
        for i, qa in enumerate(questions_answered[:10], 1):
            if not isinstance(qa, dict):
                continue
            question = str(qa.get("question", "")).strip()
            if not question:
                continue
            answer = str(qa.get("answer", "")).strip()
            qa_lines.append(f"\n{i}. <b>Q:</b> {html.escape(question)}")
            if answer:
                qa_lines.append(f"   <b>A:</b> {html.escape(answer)}")
            else:
                qa_lines.append(f"   <b>A:</b> <i>{t('no_answer', self._context.lang)}</i>")
        return "\n".join(qa_lines) if len(qa_lines) > 1 else None

    def _build_insights_messages(self, shaped: dict[str, Any]) -> list[str]:
        insights = shaped.get("insights")
        if not isinstance(insights, dict):
            return []

        messages: list[str] = []
        caution = str(insights.get("caution") or "").strip()
        if caution:
            messages.append(f"<b>⚠️ {t('caveats', self._context.lang)}</b>\n{html.escape(caution)}")

        critique = insights.get("critique")
        if isinstance(critique, list) and critique:
            crit_lines = [f"• {html.escape(str(c).strip())}" for c in critique if str(c).strip()]
            if crit_lines:
                messages.append(
                    f"<b>🤔 {t('critical_analysis', self._context.lang)}</b>\n"
                    + "\n".join(crit_lines[:5])
                )
        return messages

    def _build_quality_message(self, shaped: dict[str, Any]) -> str | None:
        quality = shaped.get("quality")
        if not isinstance(quality, dict):
            return None

        _l = self._context.lang
        lines: list[str] = []
        bias = str(quality.get("author_bias") or "").strip()
        tone = str(quality.get("emotional_tone") or "").strip()
        evidence = str(quality.get("evidence_quality") or "").strip()
        missing = quality.get("missing_perspectives")

        if bias:
            lines.append(f"• <b>{t('bias', _l)}:</b> {html.escape(bias)}")
        if tone:
            lines.append(f"• <b>{t('tone', _l)}:</b> {html.escape(tone)}")
        if evidence:
            lines.append(f"• <b>{t('evidence', _l)}:</b> {html.escape(evidence)}")
        if isinstance(missing, list) and missing:
            clean_missing = [str(m).strip() for m in missing if str(m).strip()]
            if clean_missing:
                lines.append(f"• <b>{t('missing_context', _l)}:</b>")
                lines.extend(f"  - {html.escape(item)}" for item in clean_missing[:3])

        if not lines:
            return None
        return f"<b>⚖️ {t('perspective_quality', _l)}</b>\n" + "\n".join(lines)

    def _build_taxonomy_message(self, shaped: dict[str, Any]) -> str | None:
        taxonomy = shaped.get("topic_taxonomy") or []
        if not isinstance(taxonomy, list) or not taxonomy:
            return None

        lines = [f"<b>🏷️ {t('topic_classification', self._context.lang)}</b>"]
        for tax in taxonomy[:5]:
            if not isinstance(tax, dict) or not tax.get("label"):
                continue
            label = str(tax["label"]).strip()
            score = tax.get("score", 0.0)
            if isinstance(score, (int, float)) and score > 0:
                lines.append(f"• {label} ({score:.1%})")
            else:
                lines.append(f"• {label}")
        return "\n".join(lines) if len(lines) > 1 else None

    def _build_forward_extras_message(self, shaped: dict[str, Any]) -> str | None:
        fwd_extras = shaped.get("forwarded_post_extras")
        if not isinstance(fwd_extras, dict):
            return None

        _l = self._context.lang
        fwd_parts: list[str] = []
        if fwd_extras.get("channel_title"):
            fwd_parts.append(f"📺 Channel: {fwd_extras['channel_title']}")
        if fwd_extras.get("channel_username"):
            fwd_parts.append(f"@{fwd_extras['channel_username']}")
        hashtags = self._clean_string_list(fwd_extras.get("hashtags") or [])
        if hashtags:
            fwd_parts.append(
                f"{t('tags', _l)}: "
                + " ".join(f"#{h}" if not h.startswith("#") else h for h in hashtags[:5])
            )
        if not fwd_parts:
            return None
        return f"<b>📤 {t('forward_info', _l)}</b>\n" + "\n".join(fwd_parts)

    async def send_new_field_messages(self, message: Any, shaped: dict[str, Any]) -> None:
        """Send all supplemental blocks coalesced into minimal messages."""
        try:
            combined = self.build_all_supplemental_blocks(shaped)
            if combined:
                await self._context.text_processor.send_long_text(
                    message, combined, parse_mode="HTML"
                )
        except Exception as exc:
            raise_if_cancelled(exc)
