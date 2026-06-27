"""Structured summary and forward-summary orchestration."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from app.adapters.external.formatting.html_repair import repair_html_chunk
from app.adapters.external.formatting.summary.action_buttons import create_inline_keyboard
from app.adapters.external.formatting.summary.card_renderer import (
    build_card_sections,
    build_compact_card_html,
    truncate_plain_text,
)
from app.adapters.external.formatting.summary.crosspost_publisher import crosspost_to_topic
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.core.ui_strings import t

if TYPE_CHECKING:
    from .presenter_context import SummaryPresenterContext
    from .summary_blocks import SummaryBlocksPresenter

logger = get_logger(__name__)


class StructuredSummaryFlow:
    """Handle structured-summary and forward-summary delivery."""

    def __init__(
        self,
        context: SummaryPresenterContext,
        *,
        blocks: SummaryBlocksPresenter,
    ) -> None:
        self._context = context
        self._blocks = blocks

    def _build_compact_card_html(
        self, summary_shaped: dict[str, Any], llm: Any, chunks: int | None, *, reader: bool
    ) -> str:
        return build_compact_card_html(
            summary_shaped,
            llm,
            chunks,
            reader=reader,
            text_processor=self._context.text_processor,
            data_formatter=self._context.data_formatter,
            lang=self._context.lang,
        )

    def _create_inline_keyboard(
        self,
        summary_id: int | str,
        correlation_id: str | None = None,
        source_url: str | None = None,
    ) -> Any:
        return create_inline_keyboard(
            summary_id, correlation_id, lang=self._context.lang, source_url=source_url
        )

    async def _send_action_buttons(
        self,
        message: Any,
        summary_id: int | str,
        correlation_id: str | None = None,
        source_url: str | None = None,
    ) -> int | None:
        try:
            keyboard = self._create_inline_keyboard(summary_id, correlation_id, source_url)
            if keyboard:
                msg_id = await self._context.response_sender.safe_reply_with_id(
                    message,
                    t("quick_actions", self._context.lang),
                    reply_markup=keyboard,
                )
                logger.debug("action_buttons_sent", extra={"summary_id": summary_id})
                return msg_id
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "send_action_buttons_failed",
                extra={"summary_id": summary_id, "error": str(exc)},
            )
        return None

    async def _is_reader_mode(self, message: Any) -> bool:
        if self._context.verbosity_resolver is None:
            return False
        from app.core.verbosity import VerbosityLevel

        return (
            await self._context.verbosity_resolver.get_verbosity(message)
        ) == VerbosityLevel.READER

    async def _maybe_send_cover(self, message: Any, shaped: dict[str, Any]) -> None:
        """Best-effort article cover card (source preview above the title).

        Sent before the summary to give each one a visual anchor instead of a
        wall of text. Requires a known source URL; silently skips otherwise.
        """
        url = str(shaped.get("canonical_url") or "").strip()
        if not url:
            return
        chat_id = getattr(getattr(message, "chat", None), "id", None)
        if not isinstance(chat_id, int):
            return
        metadata = shaped.get("metadata") or {}
        title = str(metadata.get("title") or "").strip() if isinstance(metadata, dict) else ""
        text = f"<b>{html.escape(title)}</b>" if title else ""
        try:
            await self._context.response_sender.send_cover_message(chat_id, text, url)
        except Exception as exc:
            raise_if_cancelled(exc)

    def _build_card_sections(
        self, summary_shaped: dict[str, Any], llm: Any, chunks: int | None, *, reader: bool
    ) -> list[str]:
        return build_card_sections(
            summary_shaped,
            llm,
            chunks,
            reader=reader,
            text_processor=self._context.text_processor,
            data_formatter=self._context.data_formatter,
            lang=self._context.lang,
        )

    async def _finalize_compact_card(
        self,
        message: Any,
        summary_shaped: dict[str, Any],
        llm: Any,
        chunks: int | None,
        summary_id: int | str | None,
        *,
        reader: bool,
    ) -> tuple[bool, str | None]:
        """Finalize progress message with header section, send remaining sections separately."""
        card_text: str | None = None
        try:
            sections = self._build_card_sections(summary_shaped, llm, chunks, reader=reader)
            # card_text = full joined card for crosspost / fallback
            card_text = "\n\n".join(sections).strip() or None

            if not sections:
                return False, card_text

            if self._context.progress_tracker is None:
                return False, card_text

            header_section = sections[0]
            remaining_sections = sections[1:]

            logger.debug(
                "card_sections_built",
                extra={
                    "section_count": len(sections),
                    "header_len": len(header_section),
                    "remaining_count": len(remaining_sections),
                    "remaining_lens": [len(s) for s in remaining_sections],
                },
            )

            # If header fits in one Telegram message, finalize with it. Use the
            # configured per-message ceiling (single source of truth) rather than
            # a separate hard-coded 4096 literal.
            _telegram_limit = self._context.text_processor.max_message_chars
            if len(header_section) <= _telegram_limit:
                # Attach keyboard to header only if no remaining sections
                keyboard = None
                if not remaining_sections and summary_id:
                    keyboard = self._create_inline_keyboard(summary_id)
                result = await self._context.progress_tracker.finalize(
                    message,
                    header_section,
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
                if result is None:
                    logger.warning(
                        "progress_finalize_failed_fallback",
                        extra={"request_message_id": getattr(message, "id", None)},
                    )
                    return False, card_text
            else:
                # Header too long (very long TLDR): finalize with just title,
                # send full TLDR separately
                await self._context.progress_tracker.finalize(
                    message,
                    repair_html_chunk(header_section[:_telegram_limit]),
                    parse_mode="HTML",
                )
                # Send overflow as a new message via send_long_text
                await self._context.text_processor.send_long_text(
                    message, header_section, parse_mode="HTML"
                )

            # Send remaining sections as separate messages.
            # Use send_long_text for all sections (handles splitting and is more
            # robust than safe_reply for potentially long HTML content).
            # Isolate each section send so one failure doesn't block the rest.
            for i, section in enumerate(remaining_sections):
                try:
                    await self._context.text_processor.send_long_text(
                        message, section, parse_mode="HTML"
                    )
                except Exception as sec_exc:
                    raise_if_cancelled(sec_exc)
                    logger.warning(
                        "card_section_send_failed",
                        extra={
                            "section_index": i + 1,
                            "section_len": len(section),
                            "error": str(sec_exc),
                        },
                    )

            # Send keyboard as a separate action buttons message.
            # Use article title as text so the chat list preview is meaningful.
            if summary_id and remaining_sections:
                keyboard = self._create_inline_keyboard(summary_id)
                if keyboard:
                    try:
                        meta = summary_shaped.get("metadata") or {}
                        _title = ""
                        _domain = ""
                        if isinstance(meta, dict):
                            _title = str(meta.get("title") or "").strip()
                            _domain = str(meta.get("domain") or "").strip()

                        # Build a compact summary receipt for the chat list preview
                        _kb_lines: list[str] = []
                        if _title:
                            _kb_lines.append(
                                f'"{truncate_plain_text(_title, 80)}" -- summary created.'
                            )

                        _meta_parts: list[str] = []
                        if _domain:
                            _meta_parts.append(_domain)
                        _rt = summary_shaped.get("estimated_reading_time_min")
                        try:
                            _rt_val = int(_rt) if _rt is not None else 0
                            if _rt_val > 0:
                                _meta_parts.append(f"~{_rt_val} min")
                        except (ValueError, TypeError):
                            pass
                        _st = str(summary_shaped.get("source_type") or "").strip().lower()
                        if _st and _st != "blog":
                            _meta_parts.append(_st.capitalize())
                        if _meta_parts:
                            _kb_lines.append(" \u00b7 ".join(_meta_parts))

                        _body = (
                            str(summary_shaped.get("summary_1000") or "").strip()
                            or str(summary_shaped.get("tldr") or "").strip()
                            or str(summary_shaped.get("summary_250") or "").strip()
                        )
                        if _body:
                            _kb_lines.append(_body)

                        kb_text = "\n".join(_kb_lines) or t("quick_actions", self._context.lang)
                        await self._context.response_sender.safe_reply(
                            message,
                            kb_text,
                            reply_markup=keyboard,
                        )
                    except Exception as kb_exc:
                        raise_if_cancelled(kb_exc)
                        logger.warning(
                            "card_keyboard_send_failed",
                            extra={"error": str(kb_exc)},
                        )

            return True, card_text
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "compact_card_build_failed",
                extra={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "request_message_id": getattr(message, "id", None),
                },
            )
        return False, card_text

    async def send_structured_summary_response(
        self,
        message: Any,
        summary_shaped: dict[str, Any],
        llm: Any,
        chunks: int | None = None,
        summary_id: int | str | None = None,
        correlation_id: str | None = None,
    ) -> int | None:
        try:
            reader = await self._is_reader_mode(message)
            if not reader:
                await self._maybe_send_cover(message, summary_shaped)
            job_card_finalized, card_text = await self._finalize_compact_card(
                message,
                summary_shaped,
                llm,
                chunks,
                summary_id,
                reader=reader,
            )

            if reader and job_card_finalized:
                return None

            # Build main content: combined lines + longest summary field + key ideas
            main_parts: list[str] = []

            if not job_card_finalized:
                combined_lines = self._blocks.build_combined_summary_lines(
                    summary_shaped, include_domain=True
                )
                if combined_lines:
                    main_parts.append("\n".join(combined_lines))

            summary_text = self._blocks.build_summary_field_text(
                summary_shaped, include_tldr=not job_card_finalized
            )
            if summary_text:
                main_parts.append(summary_text)

            ideas_text = self._blocks.build_key_ideas_text(summary_shaped)
            if ideas_text:
                main_parts.append(ideas_text)

            # Send main content as one message (text_processor handles splitting if >3500 chars)
            if main_parts:
                await self._context.text_processor.send_long_text(
                    message, "\n\n".join(main_parts), parse_mode="HTML"
                )

            # Send coalesced supplemental blocks
            await self._blocks.send_new_field_messages(message, summary_shaped)

            # Attach action buttons to a final message (or to the last sent message)
            bot_reply_id: int | None = None
            if summary_id and not job_card_finalized:
                bot_reply_id = await self._send_action_buttons(
                    message, summary_id, correlation_id, summary_shaped.get("canonical_url")
                )

            await self._crosspost_to_topic(
                message,
                summary_shaped,
                llm,
                chunks,
                summary_id,
                correlation_id,
                card_text,
            )
            return bot_reply_id
        except Exception as exc:
            raise_if_cancelled(exc)
            try:
                tl_dr = str(summary_shaped.get("summary_250", "")).strip()
                if tl_dr:
                    await self._context.response_sender.safe_reply(message, f"📋 TL;DR:\n{tl_dr}")
            except Exception as exc2:
                raise_if_cancelled(exc2)

            if summary_id:
                await self._send_action_buttons(message, summary_id, correlation_id)
            return None

    def _with_lang(self, lang: str) -> StructuredSummaryFlow:
        """Return a sibling flow that renders in ``lang``.

        The rendering helpers key every label off ``context.lang``, so a second
        language is rendered by cloning the collaborator bundle with a different
        ``lang`` rather than threading an override through every call.
        """
        if lang == self._context.lang:
            return self
        from dataclasses import replace

        from app.adapters.external.formatting.summary.summary_blocks import (
            SummaryBlocksPresenter,
        )

        ctx = replace(self._context, lang=lang)
        return StructuredSummaryFlow(ctx, blocks=SummaryBlocksPresenter(ctx))

    async def send_secondary_language_summary(
        self,
        message: Any,
        summary_shaped: dict[str, Any],
        *,
        lang: str,
        header: str | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        """Render the full summary content in a second language as new messages.

        Sends every textual field (combined lines, longest summary, key ideas,
        and supplemental blocks) translated into ``lang``. Deliberately omits the
        cover, compact-card progress finalize, action buttons, and crosspost --
        those belong to the primary-language delivery and must not be duplicated.
        Returns True when at least the main content block was sent.
        """
        flow = self._with_lang(lang)
        sent_main = False
        try:
            if header:
                await flow._context.response_sender.safe_reply(message, header)

            main_parts: list[str] = []
            combined_lines = flow._blocks.build_combined_summary_lines(
                summary_shaped, include_domain=True
            )
            if combined_lines:
                main_parts.append("\n".join(combined_lines))

            summary_text = flow._blocks.build_summary_field_text(
                summary_shaped, include_tldr=True
            )
            if summary_text:
                main_parts.append(summary_text)

            ideas_text = flow._blocks.build_key_ideas_text(summary_shaped)
            if ideas_text:
                main_parts.append(ideas_text)

            if main_parts:
                await flow._context.text_processor.send_long_text(
                    message, "\n\n".join(main_parts), parse_mode="HTML"
                )
                sent_main = True

            await flow._blocks.send_new_field_messages(message, summary_shaped)
            return sent_main
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "secondary_language_summary_failed",
                extra={"cid": correlation_id, "lang": lang, "error": str(exc)},
            )
            # Report whether the main block already reached the user so the caller
            # does not schedule a duplicate (prose) translation on top of it.
            return sent_main

    async def send_forward_summary_response(
        self, message: Any, forward_shaped: dict[str, Any], summary_id: int | str | None = None
    ) -> None:
        try:
            _l = self._context.lang
            if self._context.progress_tracker is not None:
                result = await self._context.progress_tracker.finalize(
                    message, t("forward_summary_ready", _l)
                )
                if result is None:
                    logger.warning(
                        "forward_progress_finalize_failed",
                        extra={"request_message_id": getattr(message, "id", None)},
                    )
            else:
                await self._context.response_sender.safe_reply(
                    message, t("forward_summary_ready", _l)
                )

            # Build main content: combined lines + summary + key ideas
            main_parts: list[str] = []
            combined_lines = self._blocks.build_combined_summary_lines(
                forward_shaped, include_domain=False
            )
            if combined_lines:
                main_parts.append("\n".join(combined_lines))

            summary_text = self._blocks.build_summary_field_text(forward_shaped, include_tldr=False)
            if summary_text:
                main_parts.append(summary_text)

            ideas_text = self._blocks.build_key_ideas_text(forward_shaped)
            if ideas_text:
                main_parts.append(ideas_text)

            if main_parts:
                await self._context.text_processor.send_long_text(
                    message, "\n\n".join(main_parts), parse_mode="HTML"
                )

            # Coalesced supplemental blocks
            await self._blocks.send_new_field_messages(message, forward_shaped)
        except Exception as exc:
            raise_if_cancelled(exc)

        if summary_id:
            await self._send_action_buttons(
                message, summary_id, source_url=forward_shaped.get("canonical_url")
            )

    async def _crosspost_to_topic(
        self,
        message: Any,
        summary_shaped: dict[str, Any],
        llm: Any,
        chunks: int | None,
        summary_id: int | str | None,
        correlation_id: str | None,
        card_text: str | None = None,
    ) -> None:
        if self._context.topic_manager is None:
            return
        if card_text is None:
            card_text = self._build_compact_card_html(summary_shaped, llm, chunks, reader=True)
        await crosspost_to_topic(
            topic_manager=self._context.topic_manager,
            response_sender=self._context.response_sender,
            message=message,
            summary_shaped=summary_shaped,
            summary_id=summary_id,
            correlation_id=correlation_id,
            card_text=card_text,
            create_keyboard_fn=self._create_inline_keyboard,
        )
