"""Telegram message-length limits and the expandable-blockquote band."""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock

from app.adapters.external.formatting.protocols import DataFormatter, ResponseSender
from app.adapters.external.formatting.summary.summary_blocks import SummaryBlocksPresenter
from app.adapters.external.formatting.text_processor import TextProcessorImpl
from app.config.telegram import TelegramLimitsConfig


class _SummaryTextProcessor(TextProcessorImpl):
    def __init__(self, *, max_message_chars: int) -> None:
        super().__init__(cast("ResponseSender", None), max_message_chars=max_message_chars)

    def sanitize_summary_text(self, text: str) -> str:
        return text


def test_default_chunk_ceiling_stays_under_hard_limit() -> None:
    limit = TelegramLimitsConfig().max_message_chars
    # Below Telegram's 4096 UTF-16 hard limit, with margin for entity/tag-repair
    # slack and astral chars (emoji/CJK = 2 UTF-16 units).
    assert 3500 <= limit < 4096


def test_text_processor_exposes_max_message_chars() -> None:
    tp = TextProcessorImpl(response_sender=cast("ResponseSender", None), max_message_chars=3900)
    assert tp.max_message_chars == 3900


def _presenter_with_ceiling(ceiling: int) -> SummaryBlocksPresenter:
    from app.adapters.external.formatting.summary.presenter_context import SummaryPresenterContext

    ctx = SummaryPresenterContext(
        response_sender=cast("ResponseSender", None),
        text_processor=_SummaryTextProcessor(max_message_chars=ceiling),
        data_formatter=cast("DataFormatter", None),
        verbosity_resolver=None,
        progress_tracker=None,
        topic_manager=None,
        lang="en",
    )
    return SummaryBlocksPresenter(ctx)


def test_expandable_band_tracks_configured_ceiling() -> None:
    # ceiling 2000 -> collapse band upper bound ~1900.
    presenter = _presenter_with_ceiling(2000)
    in_band = presenter.build_summary_field_text({"summary_1500": "x" * 1500}, include_tldr=False)
    assert in_band is not None and "<blockquote expandable>" in in_band
    over = presenter.build_summary_field_text({"summary_1500": "x" * 1950}, include_tldr=False)
    assert over is not None and "<blockquote" not in over  # over ceiling -> plain, splits cleanly


async def test_send_long_text_silences_trailing_chunks() -> None:
    from app.adapters.external.formatting.text_processor import TextProcessorImpl

    sender = AsyncMock()
    tp = TextProcessorImpl(sender, max_message_chars=100)
    body = ". ".join(f"sentence {i} with several words here" for i in range(40))  # multi-chunk
    await tp.send_long_text(None, body)

    calls = sender.safe_reply.await_args_list
    assert len(calls) >= 2
    assert calls[0].kwargs["silent"] is False  # first chunk notifies
    assert all(c.kwargs["silent"] is True for c in calls[1:])  # trailing chunks silent
