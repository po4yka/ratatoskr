"""Tests for CocoIndex summary-flow helpers (single-parse + payload shape)."""

from __future__ import annotations

import json

from app.infrastructure.cocoindex.flow import (
    _build_qdrant_payload,
    _coerce_summary_payload,
    _extract_indexable_text,
)


def test_coerce_parses_object_string_once() -> None:
    payload, raw = _coerce_summary_payload(json.dumps({"summary_250": "hello"}))
    assert payload == {"summary_250": "hello"}
    assert raw is None


def test_coerce_passes_through_dict() -> None:
    src = {"title": "T"}
    payload, raw = _coerce_summary_payload(src)
    assert payload is src
    assert raw is None


def test_coerce_returns_raw_fallback_for_unparseable_string() -> None:
    payload, raw = _coerce_summary_payload("not json at all")
    assert payload == {}
    assert raw == "not json at all"


def test_coerce_empty_and_non_object() -> None:
    assert _coerce_summary_payload(None) == ({}, None)
    assert _coerce_summary_payload("") == ({}, None)
    # A valid JSON non-object parses but yields no usable dict and no fallback.
    assert _coerce_summary_payload("[1, 2, 3]") == ({}, None)


def test_extract_text_prefers_title_and_first_summary() -> None:
    payload, raw = _coerce_summary_payload(
        {
            "metadata": {"title": "Title"},
            "summary_1000": "long body",
            "summary_250": "short body",
            "topic_tags": ["#ai", "#ml"],
        }
    )
    text = _extract_indexable_text(payload, raw_fallback=raw)
    assert text.startswith("Title long body")
    assert "#ai #ml" in text
    # summary_1000 wins over summary_250 (first match breaks the loop).
    assert "short body" not in text


def test_extract_text_uses_raw_fallback_on_parse_failure() -> None:
    payload, raw = _coerce_summary_payload("garbage payload")
    assert _extract_indexable_text(payload, raw_fallback=raw) == "garbage payload"


def test_extract_text_empty_payload_returns_empty() -> None:
    assert _extract_indexable_text({}, raw_fallback=None) == ""


def test_build_qdrant_payload_tags_entity_type_summary() -> None:
    payload, _ = _coerce_summary_payload(
        {"metadata": {"title": "T", "url": "https://x"}, "summary_250": "s", "topic_tags": ["#a"]}
    )
    result = _build_qdrant_payload(
        summary_id=11,
        request_id=1,
        lang="en",
        payload=payload,
        user_scope="public",
        environment="prod",
    )
    assert result["entity_type"] == "summary"
    assert result["summary_id"] == 11
    assert result["request_id"] == 1
    assert result["title"] == "T"
    assert result["url"] == "https://x"
    assert result["summary_250"] == "s"
    assert result["topic_tags"] == ["#a"]
