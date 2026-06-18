from __future__ import annotations

from typing import Any

from app.adapters.digest.formatter import (
    MAX_MESSAGE_LENGTH,
    DigestFormatter,
    _pluralize_channels,
    _pluralize_posts,
    _split_channel_entries,
)


def test_digest_formatter_returns_empty_digest_message() -> None:
    messages = DigestFormatter.format_digest([])

    assert len(messages) == 1
    assert messages[0][1] == []
    assert "Нет новых постов" in messages[0][0]


def test_digest_formatter_groups_sorts_and_attaches_buttons() -> None:
    posts = [
        {
            "_channel_username": "beta",
            "_channel_id": 20,
            "message_id": 2,
            "relevance_score": 0.2,
            "content_type": "unknown",
            "real_topic": "Beta topic",
            "tldr": "Beta summary",
            "url": "https://example.test/b",
            "key_insights": ["b1", "b2", "b3", "b4"],
        },
        {
            "_channel_username": "alpha",
            "_channel_id": 10,
            "message_id": 1,
            "relevance_score": 0.9,
            "content_type": "news",
            "real_topic": "Alpha topic",
            "tldr": "Alpha summary",
            "url": "https://example.test/a",
            "key_insights": ["a1"],
        },
        {
            "_channel_username": "alpha",
            "_channel_id": 10,
            "message_id": 3,
            "relevance_score": 0.1,
            "content_type": "tutorial",
            "tldr": "Untitled summary",
        },
    ]

    messages = DigestFormatter.format_digest(posts)

    header = messages[0][0]
    assert "3 поста" in header
    assert header.index("@alpha") < header.index("@beta")

    alpha_text, alpha_buttons = messages[1]
    assert "**@alpha**" in alpha_text
    assert "Alpha topic" in alpha_text
    assert "[Читать](https://example.test/a)" in alpha_text
    assert "a1" in alpha_text
    assert "Без темы" in alpha_text
    assert alpha_buttons[0][0]["callback_data"] == "dg:10:1"
    assert alpha_buttons[1][0]["callback_data"] == "dg:10:3"

    beta_text, beta_buttons = messages[2]
    assert "**@beta**" in beta_text
    assert "Beta summary" in beta_text
    assert "b3" in beta_text
    assert "b4" not in beta_text
    assert beta_buttons[0][0]["callback_data"] == "dg:20:2"


def test_digest_formatter_splits_large_channel_entries() -> None:
    header: tuple[str, dict[str, Any]] = ("header\n", {})
    first = ("A" * MAX_MESSAGE_LENGTH, {"text": "first", "callback_data": "dg:1:1"})
    second = ("B", {"text": "second", "callback_data": "dg:1:2"})

    chunks = _split_channel_entries([header, first, second])

    assert len(chunks) == 3
    assert chunks[0][0].startswith("header")
    assert chunks[0][1] == []
    assert chunks[1][1] == [[first[1]]]
    assert chunks[2][1] == [[second[1]]]


def test_digest_formatter_russian_pluralization() -> None:
    assert _pluralize_posts(1) == "пост"
    assert _pluralize_posts(2) == "поста"
    assert _pluralize_posts(5) == "постов"
    assert _pluralize_posts(11) == "постов"
    assert _pluralize_posts(22) == "поста"

    assert _pluralize_channels(1) == "канал"
    assert _pluralize_channels(2) == "канала"
    assert _pluralize_channels(5) == "каналов"
    assert _pluralize_channels(11) == "каналов"
    assert _pluralize_channels(22) == "канала"
