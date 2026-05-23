"""Schema-only tests for the ``x_bookmark_metadata`` model.

These tests assert structural facts about the SQLAlchemy mapping itself
(column set, index names, CHECK constraint, registry membership). They do
not exercise a live database; the round-trip migration coverage is
Step 2.3's responsibility.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import CheckConstraint, Index, Table

from app.db.models import ALL_MODELS, CORE_MODELS
from app.db.models.core import XBookmarkMetadata


def _table() -> Table:
    """Return the SQLAlchemy ``Table`` for the metadata model with the
    concrete type, so mypy understands ``.indexes`` / ``.constraints``.
    """
    return cast("Table", XBookmarkMetadata.__table__)


def test_x_bookmark_metadata_registered_in_core_models() -> None:
    assert XBookmarkMetadata in CORE_MODELS
    assert XBookmarkMetadata in ALL_MODELS


def test_x_bookmark_metadata_table_name() -> None:
    assert XBookmarkMetadata.__tablename__ == "x_bookmark_metadata"


def test_x_bookmark_metadata_columns_match_design() -> None:
    columns = {col.name for col in XBookmarkMetadata.__table__.columns}
    assert columns == {
        "request_id",
        "bookmark_external_id",
        "x_category",
        "tweet_text",
        "tweet_text_tsv",
        "tweet_author",
        "tweet_url",
        "posted_at",
        "synced_at",
    }


def test_x_bookmark_metadata_request_id_is_pk_and_fk() -> None:
    table = _table()
    request_id = table.c.request_id
    assert request_id.primary_key is True
    assert request_id.autoincrement is False
    # Single FK targeting requests.id with cascade delete.
    foreign_keys = list(request_id.foreign_keys)
    assert len(foreign_keys) == 1
    fk = foreign_keys[0]
    assert fk.column.table.name == "requests"
    assert fk.column.name == "id"
    assert fk.ondelete == "CASCADE"


def test_x_bookmark_metadata_nullability() -> None:
    table = _table()
    assert table.c.bookmark_external_id.nullable is False
    assert table.c.x_category.nullable is False
    assert table.c.tweet_text.nullable is True
    assert table.c.tweet_author.nullable is True
    assert table.c.tweet_url.nullable is False
    assert table.c.posted_at.nullable is True
    assert table.c.synced_at.nullable is False


def test_x_bookmark_metadata_has_no_lifecycle_columns() -> None:
    """Q4 lock (mem-1779534529-b37b): bookmarks are immortal once ingested."""
    columns = {col.name for col in XBookmarkMetadata.__table__.columns}
    forbidden = {"unbookmarked_at", "is_active", "deleted_at", "last_observed_at"}
    assert columns.isdisjoint(forbidden), (
        f"Lifecycle columns are forbidden by design; found: {columns & forbidden}"
    )


def test_x_bookmark_metadata_indexes_present() -> None:
    table = _table()
    index_names = {idx.name for idx in table.indexes}
    assert {
        "ix_x_bookmark_metadata_bookmark_external_id",
        "ix_x_bookmark_metadata_category",
        "ix_x_bookmark_metadata_tweet_text_tsv",
    } <= index_names


def test_bookmark_external_id_index_is_unique() -> None:
    table = _table()
    unique_index = next(
        idx
        for idx in table.indexes
        if idx.name == "ix_x_bookmark_metadata_bookmark_external_id"
    )
    assert unique_index.unique is True
    assert [col.name for col in unique_index.columns] == ["bookmark_external_id"]


def test_tweet_text_tsv_index_uses_gin() -> None:
    table = _table()
    tsv_index = next(
        idx
        for idx in table.indexes
        if idx.name == "ix_x_bookmark_metadata_tweet_text_tsv"
    )
    assert tsv_index.dialect_kwargs.get("postgresql_using") == "gin"


def test_tweet_text_tsv_is_generated_from_tweet_text() -> None:
    table = _table()
    column = table.c.tweet_text_tsv
    computed = column.computed
    assert computed is not None
    sqltext = str(computed.sqltext).lower()
    assert "to_tsvector" in sqltext
    assert "tweet_text" in sqltext
    # Stored (persisted) so the GIN index has a concrete value to ride on.
    assert computed.persisted is True


def test_category_check_constraint_pins_v2_vocabulary() -> None:
    table = _table()
    checks = [c for c in table.constraints if isinstance(c, CheckConstraint)]
    matched = [c for c in checks if c.name == "ck_x_bookmark_metadata_category"]
    assert len(matched) == 1, (
        "expected one CHECK constraint named ck_x_bookmark_metadata_category"
    )
    sqltext = str(matched[0].sqltext).lower()
    for value in (
        "tool",
        "security",
        "technique",
        "launch",
        "research",
        "opinion",
        "commerce",
    ):
        assert f"'{value}'" in sqltext, f"category {value!r} missing from CHECK constraint"


def test_index_definitions_use_real_index_objects() -> None:
    """Confirm explicit Index(...) entries land in __table_args__ — not just
    column-level index=True flags — so the migration autogen surface is stable.
    """
    table = _table()
    explicit_indexes = {idx.name for idx in table.indexes}
    assert "ix_x_bookmark_metadata_category" in explicit_indexes
    category_index = next(
        idx for idx in table.indexes if idx.name == "ix_x_bookmark_metadata_category"
    )
    assert isinstance(category_index, Index)
    assert [col.name for col in category_index.columns] == ["x_category"]


def test_synced_at_has_default() -> None:
    """``synced_at`` is non-null and should default to now() at the ORM level."""
    column = cast(
        "object",
        XBookmarkMetadata.__table__.c.synced_at,
    )
    # SQLAlchemy stores function-valued defaults on the column's default attr.
    assert getattr(column, "default", None) is not None
