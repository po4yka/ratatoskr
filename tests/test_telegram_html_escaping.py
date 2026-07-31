"""HTML-injection guards for Telegram messages sent with parse_mode="HTML".

Summary metadata/entities/ideas and cross-source relationship fields are all
derived from the LLM's summary of untrusted third-party content, so a crafted
title/tag/topic that reproduces a Telegram HTML tag must be escaped before it is
interpolated into the bot's own HTML-mode output.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from app.adapter_models.batch_analysis import RelationshipType
from app.adapters.external.formatting.protocols import DataFormatter, ResponseSender
from app.adapters.external.formatting.summary.presenter_context import SummaryPresenterContext
from app.adapters.external.formatting.summary.summary_blocks import SummaryBlocksPresenter
from app.adapters.external.formatting.text_processor import TextProcessorImpl
from app.adapters.telegram.batch_relationship_analysis_service import (
    BatchRelationshipAnalysisService,
)


class _IdentityTextProcessor(TextProcessorImpl):
    def __init__(self) -> None:
        super().__init__(cast("ResponseSender", None))

    def sanitize_summary_text(self, text: str) -> str:
        # Identity so the test observes escaping, not normalization.
        return text


def _presenter() -> SummaryBlocksPresenter:
    data_formatter = SimpleNamespace(
        format_readability=lambda _readability: None,
        format_key_stats=lambda _stats: [],
    )
    context = SummaryPresenterContext(
        response_sender=cast("ResponseSender", None),
        text_processor=_IdentityTextProcessor(),
        data_formatter=cast("DataFormatter", data_formatter),
        verbosity_resolver=None,
        progress_tracker=None,
        lang="en",
    )
    return SummaryBlocksPresenter(context)


def test_combined_summary_lines_escape_untrusted_html() -> None:
    shaped = {
        "summary_250": 'Body <b>x</b> & <a href="e">y</a>',
        "topic_tags": ["<script>", "safe"],
        "entities": {
            "people": ["<i>Alice</i>"],
            "organizations": ["A&B"],
            "locations": [],
        },
        "categories": ["<cat>"],
        "metadata": {"title": "<Dangerous>", "author": "A & B", "domain": "x.com<script>"},
    }

    out = "\n".join(_presenter().build_combined_summary_lines(shaped, include_domain=True))

    for raw in ("<b>x</b>", '<a href="e">', "<script>", "<Dangerous>", "<i>Alice</i>", "<cat>"):
        assert raw not in out, f"unescaped {raw!r} leaked into HTML output"
    assert "&lt;script&gt;" in out
    assert "&lt;Dangerous&gt;" in out
    assert "A &amp; B" in out


def test_key_ideas_escape_untrusted_html_but_keep_header() -> None:
    out = _presenter().build_key_ideas_text({"key_ideas": ["<b>idea</b>", "plain & text"]})

    assert out is not None
    assert "<b>idea</b>" not in out
    assert "&lt;b&gt;idea&lt;/b&gt;" in out
    assert "plain &amp; text" in out
    # The intentional bold header tag is preserved.
    assert out.startswith("<b>")


@pytest.mark.asyncio
async def test_batch_analysis_result_escapes_untrusted_html() -> None:
    service = BatchRelationshipAnalysisService.__new__(BatchRelationshipAnalysisService)
    formatter = SimpleNamespace(safe_reply=AsyncMock())
    service._response_formatter = formatter  # type: ignore[attr-defined]

    relationship = SimpleNamespace(
        relationship_type=RelationshipType.TOPIC_CLUSTER,
        confidence=0.9,
        reasoning="<b>reason</b>",
        series_info=None,
        cluster_info=SimpleNamespace(
            cluster_topic="<script>topic</script>",
            shared_entities=["<i>Ent</i>", "A&B"],
            shared_tags=["<tag>"],
        ),
    )
    combined_summary = SimpleNamespace(
        thematic_arc="<arc>",
        synthesized_insights=["<ins>"],
        contradictions=["<con>"],
        reading_order_rationale="<ord>",
        total_reading_time_min=5,
    )

    await service._send_batch_analysis_result(
        message=SimpleNamespace(),
        relationship=relationship,
        combined_summary=combined_summary,
        articles=[],
        language="en",
    )

    sent = formatter.safe_reply.await_args.args[1]
    for raw in (
        "<b>reason</b>",
        "<script>topic</script>",
        "<i>Ent</i>",
        "<tag>",
        "<arc>",
        "<ins>",
        "<con>",
        "<ord>",
    ):
        assert raw not in sent, f"unescaped {raw!r} leaked into HTML output"
    assert "&lt;script&gt;topic&lt;/script&gt;" in sent
    assert "A&amp;B" in sent
    # Static structural markup is preserved.
    assert "<b>Topic:</b>" in sent
    assert "<b>Thematic Arc:</b>" in sent


def test_supplemental_blocks_escape_highlights_and_key_points() -> None:
    """`_build_bullet_message` used to escape only when asked, and no caller asked.

    Both bullet lists are LLM-derived and go out under parse_mode="HTML".
    """
    out = _presenter().build_all_supplemental_blocks(
        {
            "highlights": ["<b>hl</b>", "a & b"],
            "key_points_to_remember": ['<a href="x">kp</a>'],
        }
    )

    assert out is not None
    for raw in ("<b>hl</b>", '<a href="x">kp</a>'):
        assert raw not in out, f"unescaped {raw!r} leaked into HTML output"
    assert "&lt;b&gt;hl&lt;/b&gt;" in out
    assert "a &amp; b" in out
    # The intentional bold headers survive.
    assert "<b>✨" in out


def test_taxonomy_label_is_escaped() -> None:
    out = _presenter().build_all_supplemental_blocks(
        {"topic_taxonomy": [{"label": "<i>cat</i>", "score": 0.5}, {"label": "A&B"}]}
    )

    assert out is not None
    assert "<i>cat</i>" not in out
    assert "&lt;i&gt;cat&lt;/i&gt;" in out
    assert "A&amp;B" in out


def test_forwarded_post_extras_are_escaped() -> None:
    """Channel title, username and hashtags are controlled by the source channel."""
    out = _presenter().build_all_supplemental_blocks(
        {
            "forwarded_post_extras": {
                "channel_title": "<b>Chan</b>",
                "channel_username": "user<script>",
                "hashtags": ["<tag>", "a&b"],
            }
        }
    )

    assert out is not None
    for raw in ("<b>Chan</b>", "user<script>", "<tag>"):
        assert raw not in out, f"unescaped {raw!r} leaked into HTML output"
    assert "&lt;b&gt;Chan&lt;/b&gt;" in out
    assert "&lt;script&gt;" in out
    assert "a&amp;b" in out


@pytest.mark.asyncio
async def test_russian_translation_body_is_escaped() -> None:
    """The header was escaped and the body was not, though both are one message.

    The body is raw LLM output: a translated technical article containing `<div>`
    lost that text to the Telethon parser, and an unclosed `<b>` bolded the rest.
    """
    from app.adapters.external.formatting.summary.followup_presenters import (
        SummaryFollowupPresenters,
    )

    sent: list[str] = []

    class _CapturingTextProcessor(_IdentityTextProcessor):
        async def send_long_text(
            self, message: object, text: str, *, parse_mode: str | None = None
        ) -> None:
            del message, parse_mode
            sent.append(text)

    context = SummaryPresenterContext(
        response_sender=cast("ResponseSender", None),
        text_processor=_CapturingTextProcessor(),
        data_formatter=cast("DataFormatter", SimpleNamespace()),
        verbosity_resolver=None,
        progress_tracker=None,
        lang="ru",
    )

    await SummaryFollowupPresenters(context).send_russian_translation(
        SimpleNamespace(), "Тег <div> и незакрытый <b>жирный", correlation_id=None
    )

    assert sent, "translation was never sent"
    body = sent[0]
    assert "<div>" not in body
    assert "&lt;div&gt;" in body
    assert "&lt;b&gt;" in body
    # The intentional header markup is still real markup.
    assert body.startswith("<b>")


def test_batch_progress_link_escapes_the_href_attribute() -> None:
    """The batch formatter's own escaper left `"` alone, and its output goes in href.

    A URL carrying a double quote could therefore close the attribute early and
    inject arbitrary markup into the bot's trusted progress message.
    """
    from app.adapters.external.formatting.batch_progress_formatter import BatchProgressFormatter

    link = BatchProgressFormatter._make_link('https://e.test/a"><b>x</b>', 'title"><i>y</i>')

    assert '"><b>x</b>' not in link
    assert '"><i>y</i>' not in link
    assert "&quot;" in link
    # Exactly one real anchor, opened and closed by us.
    assert link.count("<a href=") == 1
    assert link.count("</a>") == 1
