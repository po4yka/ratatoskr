"""Telegram message-length limits and the expandable-blockquote band."""

from __future__ import annotations

from app.config.telegram import TelegramLimitsConfig


def test_default_chunk_ceiling_stays_under_hard_limit() -> None:
    limit = TelegramLimitsConfig().max_message_chars
    # Below Telegram's 4096 UTF-16 hard limit, with margin for entity/tag-repair
    # slack and astral chars (emoji/CJK = 2 UTF-16 units).
    assert 3500 <= limit < 4096


def test_text_processor_exposes_max_message_chars() -> None:
    from app.adapters.external.formatting.text_processor import TextProcessorImpl

    tp = TextProcessorImpl(response_sender=None, max_message_chars=3900)  # type: ignore[arg-type]
    assert tp.max_message_chars == 3900
