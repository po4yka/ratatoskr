from __future__ import annotations

from typing import Any

import pytest

from app.infrastructure.persistence.repositories.collection_repository import (
    CollectionRepositoryAdapter,
)


class _Result:
    def __init__(self, rows: list[Any] | None = None) -> None:
        self._rows = rows or []

    def __iter__(self) -> Any:
        return iter(self._rows)

    def first(self) -> Any | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[Any]:
        return self._rows

    def scalars(self) -> _Result:
        return self


class _Session:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.executed: list[Any] = []

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def scalar(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def execute(self, stmt: Any, *_args: Any, **_kwargs: Any) -> _Result:
        self.executed.append(stmt)
        return _Result()

    async def get(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


class _Database:
    def __init__(self) -> None:
        self.session_obj = _Session()

    def session(self) -> _Session:
        return self.session_obj

    def transaction(self) -> _Session:
        return self.session_obj


@pytest.mark.asyncio
async def test_collection_repository_empty_database_paths() -> None:
    database = _Database()
    repo = CollectionRepositoryAdapter(database)  # type: ignore[arg-type]

    assert await repo.async_get_collection(1) is None
    assert await repo.async_get_collection(1, include_deleted=True) is None
    assert await repo.async_list_collections(1, None, 10, 0) == []
    assert await repo.async_list_collections(1, 2, 10, 0) == []
    assert (
        await repo.async_create_collection(
            user_id=1,
            name="Inbox",
            description=None,
            parent_id=None,
            position=1,
        )
        is None
    )
    await repo.async_update_collection(1, 1)
    await repo.async_update_collection(1, 1, name="Renamed", unknown="ignored")
    await repo.async_soft_delete_collection(1, 1)
    assert await repo.async_get_next_position(None) == 1
    assert await repo.async_get_next_position(1) == 1
    await repo.async_shift_positions(None, 1)
    await repo.async_shift_positions(1, 1)
    assert await repo.async_get_collection_tree(1) == []
    await repo.async_reorder_collections(None, [])
    await repo.async_reorder_collections(None, [{"collection_id": 1, "position": 2}])
    await repo.async_reorder_collections(2, [{"collection_id": 1, "position": 2}])
    assert await repo.async_move_collection(1, None, 1) is None
    assert await repo.async_get_item_count(1) == 0
    assert await repo.async_summary_belongs_to_user(1, 1) is False
    assert await repo.async_add_item(1, 1, 1) is False
    await repo.async_remove_item(1, 1)
    assert await repo.async_list_items(1, 10, 0) == []
    assert await repo.async_list_item_summary_ids(1, []) == []
    assert await repo.async_list_item_summary_ids(1, [1, 2]) == []
    assert await repo.async_get_next_item_position(1) == 1
    await repo.async_shift_item_positions(1, 1)
    await repo.async_reorder_items(1, [{"summary_id": 1, "position": 1}])
    assert await repo.async_bulk_set_items(1, [1, 2, 2]) == 0
    assert await repo.async_move_items(1, 2, [1, 2], None) == []
    assert await repo.async_move_items(1, 2, [1, 2], 1) == []
    assert await repo.async_get_role(1, 1) is None
    await repo.async_add_collaborator(1, 2, "viewer", invited_by=1)
    await repo.async_remove_collaborator(1, 2)
    assert await repo.async_list_collaborators(1) == []
    assert await repo.async_get_owner_info(1) is None
    assert await repo.async_create_invite(1, "viewer", None) == {}
    assert await repo.async_get_invite_by_token("token") is None
    await repo.async_update_invite(1)
    await repo.async_update_invite(1, status="revoked", ignored="value")
    assert await repo.async_accept_invite("token", 1) is None
    assert await repo.async_list_smart_collections_for_user(1) == []
    assert await repo.async_list_user_summaries_with_request(1) == []

    assert database.session_obj.added
    assert database.session_obj.executed


class _ScriptedResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScriptedResult:
        return self

    def all(self) -> list[Any]:
        return self._rows


class _ScriptedSession:
    def __init__(self, results: list[list[Any]]) -> None:
        self._results = list(results)
        self.execute_count = 0

    async def __aenter__(self) -> _ScriptedSession:
        return self

    async def __aexit__(self, *_a: object) -> None:
        return None

    async def execute(self, *_a: Any, **_k: Any) -> _ScriptedResult:
        self.execute_count += 1
        rows = self._results.pop(0) if self._results else []
        return _ScriptedResult(rows)


class _ScriptedDb:
    def __init__(self, results: list[list[Any]]) -> None:
        self.session_obj = _ScriptedSession(results)

    def session(self) -> _ScriptedSession:
        return self.session_obj

    def transaction(self) -> _ScriptedSession:
        return self.session_obj


@pytest.mark.asyncio
async def test_list_collections_query_count_is_constant() -> None:
    """async_list_collections issues O(1) queries (list + one grouped count)."""
    from app.db.models import Collection

    async def run(n: int) -> int:
        collections = [
            Collection(id=i + 1, user_id=1, name=f"c{i}", parent_id=None, position=i)
            for i in range(n)
        ]
        # grouped item counts: collection id (i+1) -> count i
        counts = [(i + 1, i) for i in range(n)]
        db = _ScriptedDb([collections, counts])
        repo = CollectionRepositoryAdapter(db)  # type: ignore[arg-type]
        result = await repo.async_list_collections(1, None, 100, 0)
        assert len(result) == n
        assert result[0]["item_count"] == 0  # collection id 1 -> count 0
        return db.session_obj.execute_count

    # One list SELECT + one grouped COUNT, regardless of collection count.
    assert await run(2) == await run(10) == 2


@pytest.mark.asyncio
async def test_reorder_items_issues_single_bulk_update() -> None:
    """async_reorder_items issues one CASE bulk UPDATE, not one UPDATE per item."""
    from sqlalchemy import Update

    from app.db.models import Collection

    class _ReorderSession:
        def __init__(self) -> None:
            self.update_count = 0

        async def __aenter__(self) -> _ReorderSession:
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

        async def scalar(self, *_a: Any, **_k: Any) -> Any:
            # _active_collection lookup -> an active collection exists.
            return Collection(id=1, user_id=1, name="c")

        async def execute(self, stmt: Any, *_a: Any, **_k: Any) -> _Result:
            if isinstance(stmt, Update):
                table = getattr(stmt, "table", None)
                # Count only the item-reorder UPDATE (collection_items), not the
                # separate parent-collection touch (collections).
                if table is not None and table.name == "collection_items":
                    self.update_count += 1
                return _Result([])
            # The existing-summary-ids SELECT -> all three requested ids exist.
            return _Result([1, 2, 3])

    class _ReorderDb:
        def __init__(self, session: _ReorderSession) -> None:
            self.session_obj = session

        def session(self) -> _ReorderSession:
            return self.session_obj

        def transaction(self) -> _ReorderSession:
            return self.session_obj

    sess = _ReorderSession()
    repo = CollectionRepositoryAdapter(_ReorderDb(sess))  # type: ignore[arg-type]
    await repo.async_reorder_items(1, [{"summary_id": i, "position": i} for i in (1, 2, 3)])
    assert sess.update_count == 1


@pytest.mark.asyncio
async def test_collection_repository_rejects_missing_parent() -> None:
    repo = CollectionRepositoryAdapter(_Database())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="parent collection 404 not found"):
        await repo.async_create_collection(
            user_id=1,
            name="Child",
            description=None,
            parent_id=404,
            position=1,
        )
