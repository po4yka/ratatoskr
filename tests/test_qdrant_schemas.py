"""Unit tests for QdrantQueryFilters — no Qdrant server required."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError
from qdrant_client.models import Filter

from app.infrastructure.vector.qdrant_schemas import QdrantQueryFilters


def _must(f: Filter) -> list[Any]:
    return list(f.must or [])


# ---------------------------------------------------------------------------
# Validation / normalisation
# ---------------------------------------------------------------------------


def test_minimal_filter_produces_env_and_scope() -> None:
    f = QdrantQueryFilters(environment="dev", user_scope="public").to_filter()
    must = _must(f)
    assert any(c.key == "environment" and c.match.value == "dev" for c in must)
    assert any(c.key == "user_scope" and c.match.value == "public" for c in must)
    assert len(must) == 2


def test_sanitize_scope_strips_special_chars() -> None:
    fq = QdrantQueryFilters(environment="prod env!", user_scope="user@scope")
    assert fq.environment == "prodenv"
    assert fq.user_scope == "userscope"


def test_sanitize_scope_empty_raises() -> None:
    with pytest.raises(ValueError, match="environment"):
        QdrantQueryFilters(environment="", user_scope="public")


def test_language_adds_condition() -> None:
    f = QdrantQueryFilters(environment="dev", user_scope="public", language="ru").to_filter()
    must = _must(f)
    assert any(c.key == "language" and c.match.value == "ru" for c in must)
    assert len(must) == 3


def test_request_id_condition() -> None:
    f = QdrantQueryFilters(environment="dev", user_scope="public", request_id=42).to_filter()
    must = _must(f)
    assert any(c.key == "request_id" and c.match.value == 42 for c in must)


def test_summary_id_condition() -> None:
    f = QdrantQueryFilters(environment="dev", user_scope="public", summary_id=7).to_filter()
    must = _must(f)
    assert any(c.key == "summary_id" and c.match.value == 7 for c in must)


def test_user_id_condition() -> None:
    f = QdrantQueryFilters(environment="dev", user_scope="public", user_id=1001).to_filter()
    must = _must(f)
    assert any(c.key == "user_id" and c.match.value == 1001 for c in must)


# ---------------------------------------------------------------------------
# Tag handling
# ---------------------------------------------------------------------------


def test_tags_strip_hash_prefix() -> None:
    fq = QdrantQueryFilters(environment="dev", user_scope="public", tags=["#ai", "ml"])
    assert fq.tags == ["ai", "ml"]


def test_tags_each_add_match_any_condition() -> None:
    f = QdrantQueryFilters(environment="dev", user_scope="public", tags=["ai", "ml"]).to_filter()
    must = _must(f)
    tag_conditions = [c for c in must if c.key == "tags"]
    assert len(tag_conditions) == 2
    assert all(hasattr(c.match, "any") for c in tag_conditions)
    assert any(c.match.any == ["ai"] for c in tag_conditions)
    assert any(c.match.any == ["ml"] for c in tag_conditions)


def test_tags_deduplication() -> None:
    fq = QdrantQueryFilters(environment="dev", user_scope="public", tags=["ai", "ai", "ml"])
    assert fq.tags == ["ai", "ml"]


def test_tags_none_produces_empty() -> None:
    fq = QdrantQueryFilters(environment="dev", user_scope="public", tags=None)
    assert fq.tags == []


def test_tags_set_input() -> None:
    fq = QdrantQueryFilters(environment="dev", user_scope="public", tags={"python", "async"})  # type: ignore[arg-type]
    assert set(fq.tags) == {"python", "async"}


def test_single_string_tag() -> None:
    fq = QdrantQueryFilters(environment="dev", user_scope="public", tags="rust")  # type: ignore[arg-type]
    assert fq.tags == ["rust"]


# ---------------------------------------------------------------------------
# All-fields combined
# ---------------------------------------------------------------------------


def test_full_filter_condition_count() -> None:
    f = QdrantQueryFilters(
        environment="staging",
        user_scope="private",
        language="en",
        tags=["a", "b"],
        request_id=1,
        summary_id=2,
        user_id=3,
    ).to_filter()
    # env + scope + language + req + summary + user + 2 tags = 8
    assert len(_must(f)) == 8


# ---------------------------------------------------------------------------
# Validation guards
# ---------------------------------------------------------------------------


def test_request_id_negative_raises() -> None:
    with pytest.raises(ValidationError):
        QdrantQueryFilters(environment="dev", user_scope="public", request_id=-1)


def test_user_id_zero_raises() -> None:
    with pytest.raises(ValidationError):
        QdrantQueryFilters(environment="dev", user_scope="public", user_id=0)


def test_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        QdrantQueryFilters(environment="dev", user_scope="public", unknown_field="x")  # type: ignore[call-arg]
