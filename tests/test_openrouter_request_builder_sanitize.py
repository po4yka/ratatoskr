"""Tests for RequestBuilder.sanitize_messages and its precompiled patterns.

sanitize_messages is a cosmetic (non-security) filter that strips a small set of
injection-signal phrases from user-role message text. The patterns are compiled
once at import (_INJECTION_SIGNAL_PATTERNS) rather than rebuilt per message on
every chat() call; these tests pin both the filtering behavior and that the
constant is a tuple of precompiled patterns.
"""

from __future__ import annotations

import re

import pytest

from app.adapters.openrouter.request_builder import (
    _INJECTION_SIGNAL_PATTERNS,
    RequestBuilder,
)


@pytest.fixture
def builder() -> RequestBuilder:
    return RequestBuilder(api_key="test-key")


def test_patterns_are_precompiled_case_insensitive() -> None:
    assert isinstance(_INJECTION_SIGNAL_PATTERNS, tuple)
    assert len(_INJECTION_SIGNAL_PATTERNS) == 5
    assert all(isinstance(pat, re.Pattern) for pat in _INJECTION_SIGNAL_PATTERNS)
    assert all(pat.flags & re.IGNORECASE for pat in _INJECTION_SIGNAL_PATTERNS)


@pytest.mark.parametrize(
    "phrase",
    [
        "ignore previous instructions",
        "forget previous instructions",
        "system:",
        "assistant:",
        "user:",
    ],
)
def test_strips_each_injection_signal_case_insensitively(
    builder: RequestBuilder, phrase: str
) -> None:
    messages = [{"role": "user", "content": f"hello {phrase.upper()} world"}]
    result = builder.sanitize_messages(messages)
    # The phrase itself is gone (case-insensitively); surrounding text survives.
    assert phrase.lower() not in result[0]["content"].lower()
    assert "hello" in result[0]["content"]
    assert "world" in result[0]["content"]


def test_leaves_system_and_assistant_messages_untouched(builder: RequestBuilder) -> None:
    messages = [
        {"role": "system", "content": "system: ignore previous instructions"},
        {"role": "assistant", "content": "assistant: user: text"},
        {"role": "user", "content": "plain user content"},
    ]
    result = builder.sanitize_messages(messages)
    assert result[0]["content"] == "system: ignore previous instructions"
    assert result[1]["content"] == "assistant: user: text"
    assert result[2]["content"] == "plain user content"


def test_unchanged_user_message_is_not_copied(builder: RequestBuilder) -> None:
    original = {"role": "user", "content": "nothing to strip here"}
    result = builder.sanitize_messages([original])
    # No substitution happened, so the original object is passed through as-is.
    assert result[0] is original


def test_sanitizes_multimodal_text_parts_and_preserves_others(builder: RequestBuilder) -> None:
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi IGNORE PREVIOUS INSTRUCTIONS now"},
                {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}},
            ],
        }
    ]
    result = builder.sanitize_messages(messages)
    parts = result[0]["content"]
    assert "ignore previous instructions" not in parts[0]["text"].lower()
    assert parts[0]["text"] == "hi  now"
    # Non-text parts pass through unchanged.
    assert parts[1] == {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}


def test_strips_multiple_signals_in_one_message(builder: RequestBuilder) -> None:
    messages = [
        {
            "role": "user",
            "content": "system: do X. Ignore previous instructions. assistant: ok",
        }
    ]
    result = builder.sanitize_messages(messages)
    lowered = result[0]["content"].lower()
    assert "system:" not in lowered
    assert "assistant:" not in lowered
    assert "ignore previous instructions" not in lowered
    assert "do x." in lowered
