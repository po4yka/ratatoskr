"""The id a caller computes must be the id Qdrant actually stores.

Two kinds of identifier reach the vector store. Summaries and wiki pages hand it
a raw domain key that still needs hashing; repositories and git mirrors hand it a
value the helpers in ``point_ids`` already turned into a UUID. The store used to
hash both, so the second kind was stored as the hash of a hash and no caller
could address it again.

Nothing failed loudly: search matches on the vector and the payload, not the id,
so the only symptom was that every delete missed and left its point behind. These
tests pin the round trip in both directions.
"""

from __future__ import annotations

from typing import Any

from app.infrastructure.vector.point_ids import (
    git_mirror_point_id,
    normalize_point_id,
    repository_point_id,
    str_to_uuid,
    summary_point_id,
)
from app.infrastructure.vector.qdrant_store import QdrantVectorStore

ENV = "production"
SCOPE = "public"


def test_helper_produced_ids_survive_normalization_unchanged() -> None:
    """A UUID from the helpers is already the point id -- do not hash it again."""
    for point_id in (
        repository_point_id(ENV, SCOPE, 1114),
        git_mirror_point_id(ENV, SCOPE, 41),
        summary_point_id(1585, 1406),
    ):
        assert normalize_point_id(point_id) == point_id


def test_raw_domain_keys_are_still_hashed() -> None:
    """Summary and wiki callers pass an unhashed key; that behaviour is unchanged."""
    assert normalize_point_id("1585:1406") == str_to_uuid("1585:1406")
    assert normalize_point_id("/x_wiki/library/note.md") == str_to_uuid("/x_wiki/library/note.md")


def test_normalization_is_idempotent() -> None:
    once = normalize_point_id("1585:1406")
    assert normalize_point_id(once) == once


def test_summary_fast_path_and_helper_agree() -> None:
    """The fast path passes the raw key; the reconciler computes summary_point_id."""
    assert normalize_point_id("1585:1406") == summary_point_id(1585, 1406)


def _build_one(raw_id: str) -> Any:
    """Run _build_points without touching a live Qdrant client."""
    store = object.__new__(QdrantVectorStore)
    store._environment = ENV
    store._user_scope = SCOPE
    points = QdrantVectorStore._build_points(store, [[0.1, 0.2]], [{"entity_type": "x"}], [raw_id])
    return points[0]


def test_stored_repository_id_matches_what_a_deleter_computes() -> None:
    """The regression: delete_repository_point addresses repository_point_id."""
    expected = repository_point_id(ENV, SCOPE, 1114)
    assert str(_build_one(expected).id) == expected


def test_stored_summary_id_matches_the_shared_namespace() -> None:
    """The fast path hands over "<request_id>:<summary_id>" and must land on the
    same point the reconciler addresses by summary_point_id."""
    assert str(_build_one("1585:1406").id) == summary_point_id(1585, 1406)
