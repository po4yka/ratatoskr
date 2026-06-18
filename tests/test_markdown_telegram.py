"""Markdown -> Telegram-HTML renderer + expandable-blockquote integration."""

from __future__ import annotations

from typing import cast

import pytest

from app.adapters.external.formatting.protocols import DataFormatter, ResponseSender
from app.adapters.external.formatting.markdown_telegram import (
    EXPANDABLE_MIN_CHARS,
    blockquote,
    maybe_expandable_blockquote,
    render_markdown,
)
from app.adapters.external.formatting.summary.presenter_context import SummaryPresenterContext
from app.adapters.external.formatting.text_processor import TextProcessorImpl


class _SummaryTextProcessor(TextProcessorImpl):
    def __init__(self) -> None:
        super().__init__(cast("ResponseSender", None))

    def sanitize_summary_text(self, text: str) -> str:
        return text


def test_inline_formatting_maps_to_telegram_tags() -> None:
    out = render_markdown("Some **bold**, _italic_, ~~strike~~ and `code`.")
    assert "<b>bold</b>" in out
    assert "<i>italic</i>" in out
    assert "<s>strike</s>" in out
    assert "<code>code</code>" in out


def test_nested_emphasis_is_handled_correctly() -> None:
    # The old regex converter mangled this; markdown-it nests it properly.
    assert render_markdown("**bold _and italic_**") == "<b>bold <i>and italic</i></b>"


def test_headings_degrade_to_bold_lines() -> None:
    assert render_markdown("# Title") == "<b>▶ Title</b>"
    assert render_markdown("## Sub") == "<b>📌 Sub</b>"
    assert render_markdown("### Small") == "<b>Small</b>"


def test_links_only_keep_safe_schemes() -> None:
    out = render_markdown("[ok](https://x.com) [js](javascript:alert(1)) [d](data:x)")
    assert '<a href="https://x.com">ok</a>' in out
    # Unsafe schemes never become anchors.
    assert 'href="javascript' not in out
    assert 'href="data:' not in out


def test_text_content_is_escaped() -> None:
    out = render_markdown("a < b & c > d and <script>")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp;" in out


def test_lists_render_as_bullets_and_numbers() -> None:
    assert render_markdown("- one\n- two") == "• one\n• two"
    assert render_markdown("1. first\n2. second") == "1. first\n2. second"


def test_fenced_code_keeps_language_hint() -> None:
    out = render_markdown("```python\nprint('hi')\n```")
    assert out == "<pre><code class=\"language-python\">print('hi')</code></pre>"


def test_blockquote_is_expandable_only_when_long() -> None:
    short = render_markdown("> short quote")
    assert short == "<blockquote>short quote</blockquote>"
    long_quote = render_markdown("> " + ("word " * 200))
    assert long_quote.startswith("<blockquote expandable>")


def test_helpers() -> None:
    assert blockquote("x") == "<blockquote>x</blockquote>"
    assert blockquote("x", expandable=True) == "<blockquote expandable>x</blockquote>"
    assert blockquote("a & b") == "<blockquote>a &amp; b</blockquote>"
    assert "expandable" not in maybe_expandable_blockquote("tiny")
    assert "expandable" in maybe_expandable_blockquote("x" * (EXPANDABLE_MIN_CHARS + 1))


def test_empty_input_is_empty() -> None:
    assert render_markdown("") == ""
    assert render_markdown("   \n  ") == ""


def test_output_round_trips_through_telethon_html_parser() -> None:
    """Rendered HTML must be valid Telegram markup with a real expandable flag."""
    thtml = pytest.importorskip("telethon.extensions.html")
    md = (
        "# Title\n\n**bold** and [link](https://x.com)\n\n"
        "> " + ("long " * 200) + "\n\n- a\n- b\n\n```py\nx=1\n```"
    )
    _text, entities = thtml.parse(render_markdown(md))  # must not raise
    quotes = [e for e in entities if type(e).__name__ == "MessageEntityBlockquote"]
    assert quotes and any(getattr(e, "collapsed", None) for e in quotes)


def _field_context() -> SummaryPresenterContext:
    return SummaryPresenterContext(
        response_sender=cast("ResponseSender", None),
        text_processor=_SummaryTextProcessor(),
        data_formatter=cast("DataFormatter", None),
        verbosity_resolver=None,
        progress_tracker=None,
        topic_manager=None,
        lang="en",
    )


def test_summary_field_collapses_long_body_and_escapes_short() -> None:
    from app.adapters.external.formatting.summary.summary_blocks import SummaryBlocksPresenter

    presenter = SummaryBlocksPresenter(_field_context())

    long_body = "x " * (EXPANDABLE_MIN_CHARS + 50)
    long_out = presenter.build_summary_field_text({"summary_1500": long_body}, include_tldr=False)
    assert long_out is not None and "<blockquote expandable>" in long_out

    short_out = presenter.build_summary_field_text({"summary_50": "a < b & c"}, include_tldr=False)
    assert short_out is not None
    assert "<blockquote" not in short_out
    assert "a &lt; b &amp; c" in short_out  # short bodies stay inline but escaped


def test_oversized_body_is_not_collapsed() -> None:
    # Past one message, the quote would split and only the first chunk collapses,
    # so a body over EXPANDABLE_MAX_CHARS stays a plain (non-expandable) quote/text.
    from app.adapters.external.formatting.markdown_telegram import (
        EXPANDABLE_MAX_CHARS,
        maybe_expandable_blockquote,
    )

    huge = "x" * (EXPANDABLE_MAX_CHARS + 100)
    assert "expandable" not in maybe_expandable_blockquote(huge)
    # And a huge markdown blockquote renders as a plain (non-expandable) quote.
    assert render_markdown("> " + huge).startswith("<blockquote>")


def test_summary_field_does_not_collapse_oversized_tldr() -> None:
    from app.adapters.external.formatting.markdown_telegram import EXPANDABLE_MAX_CHARS
    from app.adapters.external.formatting.summary.summary_blocks import SummaryBlocksPresenter

    presenter = SummaryBlocksPresenter(_field_context())
    oversized = "word " * EXPANDABLE_MAX_CHARS  # well over the per-message limit
    out = presenter.build_summary_field_text({"tldr": oversized}, include_tldr=True)
    assert out is not None
    assert "<blockquote" not in out  # left plain so it chunks cleanly, like before


def test_code_block_nested_in_list_is_not_indentation_corrupted() -> None:
    out = render_markdown("- item\n\n  ```py\n  a=1\n  b=2\n  ```")
    # The code body must not gain spurious leading spaces on continuation lines.
    assert "a=1\nb=2" in out
    assert "a=1\n   b=2" not in out


def test_nested_bullet_list_still_indents() -> None:
    # The indent fix must not regress real nested-list indentation.
    out = render_markdown("- parent\n  - child")
    assert "• parent" in out
    assert "   • child" in out  # nested child stays indented under its parent
