"""Follow-up summary presenters for translations and extra outputs."""

from __future__ import annotations

import html
import re
from typing import TYPE_CHECKING, Any

from app.adapters.external.formatting.summary.related_reads_presenter import (
    send_related_reads as present_related_reads,
)
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.core.ui_strings import t

if TYPE_CHECKING:
    from app.application.services.related_reads_service import RelatedReadItem

    from .presenter_context import SummaryPresenterContext

logger = get_logger(__name__)


class SummaryFollowupPresenters:
    """Handle follow-up presentation flows beyond structured summaries."""

    def __init__(self, context: SummaryPresenterContext) -> None:
        self._context = context

    async def send_russian_translation(
        self, message: Any, translated_text: str, correlation_id: str | None = None
    ) -> None:
        if not translated_text or not translated_text.strip():
            logger.warning("russian_translation_empty", extra={"cid": correlation_id})
            return

        cleaned = self._context.text_processor.sanitize_summary_text(translated_text.strip())
        header = "Перевод резюме"

        reader = False
        if self._context.verbosity_resolver is not None:
            from app.core.verbosity import VerbosityLevel

            reader = (
                await self._context.verbosity_resolver.get_verbosity(message)
            ) == VerbosityLevel.READER

        if correlation_id and not reader:
            header += f"\nCorrelation ID: {correlation_id}"

        # The body needs escaping as much as the header does: it is raw LLM
        # output, and sanitize_summary_text only NFC-normalizes and strips
        # control characters. A translation of a technical article carrying
        # `<div>` lost that text to the Telethon parser, and an unclosed `<b>`
        # bolded the rest of the message. The sibling renderer in
        # summary_blocks escapes its bodies.
        await self._context.text_processor.send_long_text(
            message,
            f"<b>{html.escape(header)}</b>\n\n{html.escape(cleaned)}",
            parse_mode="HTML",
        )

    def _insights_cap_text(self, text: str, max_chars: int) -> str:
        cleaned = self._context.text_processor.sanitize_summary_text(text.strip())
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[: max(0, max_chars - 1)].rstrip() + "…"

    def _insights_safe_html(self, text: str, *, max_chars: int = 900) -> str:
        return self._context.text_processor.linkify_urls(
            html.escape(self._insights_cap_text(text, max_chars))
        )

    def _insights_clean_list(
        self, items: list[Any], *, limit: int, item_max_chars: int = 220
    ) -> list[str]:
        cleaned: list[str] = []
        for item in items:
            text = str(item).strip()
            if not text:
                continue
            cleaned.append(self._insights_safe_html(text, max_chars=item_max_chars))
            if len(cleaned) >= limit:
                break
        return cleaned

    def _build_new_facts_section(self, insights: dict[str, Any]) -> list[str]:
        facts_section: list[str] = []
        facts = insights.get("new_facts")
        if not isinstance(facts, list):
            return facts_section

        _l = self._context.lang
        for idx, fact in enumerate(facts[:5], start=1):
            if not isinstance(fact, dict):
                continue
            fact_text = str(fact.get("fact", "")).strip()
            if not fact_text:
                continue

            fact_lines = [f"<b>{idx}.</b> {self._insights_safe_html(fact_text, max_chars=320)}"]
            why_matters = str(fact.get("why_it_matters", "")).strip()
            if why_matters:
                fact_lines.append(
                    f"• <i>{t('why_matters', _l)}:</i> "
                    f"{self._insights_safe_html(why_matters, max_chars=260)}"
                )

            source_hint = str(fact.get("source_hint", "")).strip()
            if source_hint:
                fact_lines.append(
                    f"• <i>{t('source_hint', _l)}:</i> "
                    f"{self._insights_safe_html(source_hint, max_chars=160)}"
                )

            confidence = fact.get("confidence")
            if confidence is not None:
                try:
                    conf_val = float(confidence)
                    fact_lines.append(f"• <i>Confidence:</i> <code>{conf_val:.0%}</code>")
                except Exception:
                    logger.debug("confidence_score_conversion_failed", exc_info=True)
                    fact_lines.append(
                        f"• <i>Confidence:</i> <code>{html.escape(str(confidence))}</code>"
                    )

            facts_section.append("\n".join(fact_lines))
        return facts_section

    async def send_additional_insights_message(
        self, message: Any, insights: dict[str, Any], correlation_id: str | None = None
    ) -> None:
        try:
            if await self._is_reader_mode(message):
                return
            _l = self._context.lang
            lines: list[str] = [f"<b>🔎 {t('research_highlights', _l)}</b>"]
            if correlation_id:
                lines.append(
                    f"<i>Correlation ID:</i> <code>{html.escape(str(correlation_id))}</code>"
                )

            sections_sent = False

            overview = insights.get("topic_overview")
            if isinstance(overview, str) and overview.strip():
                sections_sent = True
                lines.extend(
                    [
                        "",
                        f"<b>🧭 {t('overview', _l)}</b>",
                        self._insights_safe_html(overview, max_chars=1200),
                    ]
                )

            facts_section = self._build_new_facts_section(insights)
            if facts_section:
                sections_sent = True
                lines.extend(["", f"<b>📌 {t('fresh_facts', _l)}</b>", "\n\n".join(facts_section)])

            open_questions = insights.get("open_questions")
            if isinstance(open_questions, list):
                questions = self._insights_clean_list(open_questions, limit=5)
                if questions:
                    sections_sent = True
                    lines.extend(
                        [
                            "",
                            f"<b>❓ {t('open_questions', _l)}</b>",
                            "\n".join(f"• {q}" for q in questions),
                        ]
                    )

            suggested_sources = insights.get("suggested_sources")
            if isinstance(suggested_sources, list):
                sources = self._insights_clean_list(suggested_sources, limit=5, item_max_chars=260)
                if sources:
                    sections_sent = True
                    lines.extend(
                        [
                            "",
                            f"<b>🔗 {t('suggested_followup', _l)}</b>",
                            "\n".join(f"• {s}" for s in sources),
                        ]
                    )

            expansion = insights.get("expansion_topics")
            if isinstance(expansion, list):
                exp_clean = self._insights_clean_list(expansion, limit=6)
                if exp_clean:
                    sections_sent = True
                    lines.extend(
                        [
                            "",
                            f"<b>🧠 {t('expansion_topics', _l)}</b> ({t('beyond_article', _l)})",
                            "\n".join(f"• {item}" for item in exp_clean),
                        ]
                    )

            next_steps = insights.get("next_exploration")
            if isinstance(next_steps, list):
                nxt_clean = self._insights_clean_list(next_steps, limit=6)
                if nxt_clean:
                    sections_sent = True
                    lines.extend(
                        [
                            "",
                            f"<b>🚀 {t('explore_next', _l)}</b>",
                            "\n".join(f"• {step}" for step in nxt_clean),
                        ]
                    )

            caution = insights.get("caution")
            if isinstance(caution, str) and caution.strip():
                sections_sent = True
                lines.extend(
                    [
                        "",
                        f"<b>⚠️ {t('caveats', _l)}</b>",
                        self._insights_safe_html(caution, max_chars=900),
                    ]
                )

            if not sections_sent:
                await self._context.response_sender.safe_reply(message, t("no_insights", _l))
                return

            await self._context.text_processor.send_long_text(
                message,
                "\n".join(lines).strip(),
                parse_mode="HTML",
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.error("insights_message_error", extra={"error": str(exc), "cid": correlation_id})

    async def send_custom_article(self, message: Any, article: dict[str, Any]) -> None:
        try:
            title = str(article.get("title", "")).strip() or "Custom Article"
            subtitle = str(article.get("subtitle", "") or "").strip()
            body = str(article.get("article_markdown", "")).strip()

            raw_highlights = article.get("highlights")
            if isinstance(raw_highlights, list):
                highlights = [str(x).strip() for x in raw_highlights if str(x).strip()]
            elif isinstance(raw_highlights, str):
                highlights = [
                    part.strip(" -•\t")
                    for part in re.split(r"[\n\r•;]+", raw_highlights)
                    if part.strip()
                ]
            elif raw_highlights is None:
                highlights = []
            else:
                highlights = [str(raw_highlights).strip()] if str(raw_highlights).strip() else []

            header_lines: list[str] = [f"<b>📝 {html.escape(title)}</b>"]
            if subtitle:
                header_lines.append(f"<i>{html.escape(subtitle)}</i>")

            await self._context.response_sender.safe_reply(
                message,
                "\n".join(header_lines),
                parse_mode="HTML",
            )

            if body:
                await self._context.text_processor.send_long_text(message, body)

            if highlights:
                await self._context.text_processor.send_long_text(
                    message,
                    f"⭐ {t('key_highlights', self._context.lang)}:\n"
                    + "\n".join([f"• {h}" for h in highlights[:10]]),
                )

            await self._context.response_sender.reply_json(message, article)
        except Exception as exc:
            raise_if_cancelled(exc)

    async def send_related_reads(
        self,
        message: Any,
        items: list[RelatedReadItem],
        *,
        lang: str | None = None,
    ) -> None:
        await present_related_reads(
            self._context.response_sender,
            message,
            items,
            lang or self._context.lang,
        )

    async def _is_reader_mode(self, message: Any) -> bool:
        if self._context.verbosity_resolver is None:
            return False
        from app.core.verbosity import VerbosityLevel

        return (
            await self._context.verbosity_resolver.get_verbosity(message)
        ) == VerbosityLevel.READER
