"""T6: byte-compat summary point helpers + the index adapter's empty-text skip."""

from __future__ import annotations

from typing import Any

import pytest

from app.application.dto.vector_search import RetrievalScope
from app.infrastructure.vector.summary_index_adapter import QdrantSummaryIndexAdapter
from app.infrastructure.vector.summary_point import (
    build_summary_qdrant_payload,
    coerce_summary_payload,
    extract_indexable_text,
)


def test_coerce_handles_none_dict_valid_and_invalid_json() -> None:
    assert coerce_summary_payload(None) == ({}, None)
    assert coerce_summary_payload({"title": "x"}) == ({"title": "x"}, None)
    assert coerce_summary_payload('{"title": "x"}') == ({"title": "x"}, None)
    # Unparseable string -> empty dict + truncated raw fallback for embedding.
    payload, raw = coerce_summary_payload("not json")
    assert payload == {} and raw == "not json"
    # Valid JSON that isn't an object -> empty dict, no fallback.
    assert coerce_summary_payload("[1, 2]") == ({}, None)


def test_extract_indexable_text_precedence_and_fallback() -> None:
    assert extract_indexable_text({}, raw_fallback="raw") == "raw"
    # title (from metadata) + first present of summary_1000/250/tldr + tags.
    text = extract_indexable_text(
        {
            "metadata": {"title": "Title"},
            "summary_250": "short",
            "tldr": "long",
            "topic_tags": ["#a", "#b"],
        }
    )
    assert text == "Title short #a #b"  # summary_250 wins over tldr; no summary_1000


def test_build_payload_has_exact_12_keys_and_defaults() -> None:
    payload = build_summary_qdrant_payload(10, 1, None, {}, "unit", "test")
    assert set(payload) == {
        "entity_type",
        "summary_id",
        "request_id",
        "language",
        "user_scope",
        "environment",
        "title",
        "url",
        "source_type",
        "tldr",
        "topic_tags",
        "summary_250",
    }
    assert payload["language"] == "en"  # lang None defaults to "en" (payload key only)
    assert payload["title"] == "" and payload["topic_tags"] == []


class _RecordingStore:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def replace_summary_point(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


class _FakeEmbedding:
    async def generate_embedding(self, text: str, **_kw: Any) -> list[float]:
        return [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_index_adapter_skips_when_text_empty() -> None:
    store = _RecordingStore()
    adapter = QdrantSummaryIndexAdapter(vector_store=store, embedding_service=_FakeEmbedding())
    # A summary with no embeddable text -> nothing to index, no upsert.
    await adapter.index_summary(
        request_id=1,
        summary_id=2,
        summary={},
        lang="en",
        scope=RetrievalScope(environment="test", user_scope="unit", user_id=None),
    )
    assert store.calls == []
