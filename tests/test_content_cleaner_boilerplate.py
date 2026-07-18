"""Tests for _remove_boilerplate_sections and its precompiled heading pattern.

The function skips a boilerplate section (e.g. "Related Articles", "Comments")
until the next real markdown heading. The "next heading" check is a precompiled
pattern (_HEADING_LINE) applied to the raw, unstripped line -- these tests pin
both the skipping behavior and that edge semantics, plus equivalence with the
original inline regex.
"""

from __future__ import annotations

import re

import pytest

from app.core.content_cleaner import _HEADING_LINE, _remove_boilerplate_sections


def test_heading_line_is_precompiled() -> None:
    assert isinstance(_HEADING_LINE, re.Pattern)
    assert _HEADING_LINE.pattern == r"^#{1,4}\s+\S"


def test_removes_boilerplate_section_until_next_heading() -> None:
    text = "\n".join(
        [
            "# Real Article",
            "Body paragraph.",
            "## Related Articles",
            "- link one",
            "- link two",
            "## Conclusion",
            "Final thoughts.",
        ]
    )
    result = _remove_boilerplate_sections(text)
    assert result.split("\n") == [
        "# Real Article",
        "Body paragraph.",
        "## Conclusion",
        "Final thoughts.",
    ]


def test_indented_heading_does_not_stop_skipping() -> None:
    # The next-heading check runs against the raw (unstripped) line, so an
    # indented "heading" does not count and the section keeps being skipped.
    text = "\n".join(
        [
            "## Comments",
            "some comment",
            "  ## Indented (not a real heading at column 0)",
            "### Real Heading",
            "kept body",
        ]
    )
    result = _remove_boilerplate_sections(text)
    assert result.split("\n") == ["### Real Heading", "kept body"]


def test_no_boilerplate_is_a_noop() -> None:
    text = "# Title\n\nParagraph one.\n\n## Details\nMore text."
    assert _remove_boilerplate_sections(text) == text


@pytest.mark.parametrize(
    "line",
    [
        "# Heading",
        "#### Deep",
        "##NoSpace",
        "  ## Indented",
        "not a heading",
        "##### Too many hashes",
        "",
        "#",
        "## ",
    ],
)
def test_heading_line_matches_original_inline_regex(line: str) -> None:
    # Equivalence with the pre-refactor inline pattern (re.match, no flags).
    assert bool(_HEADING_LINE.match(line)) == bool(re.match(r"^#{1,4}\s+\S", line))
