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
    await repo.async_update_collection(1)
    await repo.async_update_collection(1, name="Renamed", unknown="ignored")
    await repo.async_soft_delete_collection(1)
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
