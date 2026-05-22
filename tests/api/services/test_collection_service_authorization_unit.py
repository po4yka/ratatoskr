from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.api.exceptions import ResourceNotFoundError
from app.api.services import collection_service as collection_service_module
from app.api.services.collection_service import CollectionService


class _FakeCollectionRepository:
    def __init__(self) -> None:
        self.async_get_collection = AsyncMock(
            return_value={"id": 10, "collection_type": "manual", "user_id": 1001}
        )
        self.async_get_role = AsyncMock(return_value="editor")
        self.async_summary_belongs_to_user = AsyncMock(return_value=True)
        self.async_get_next_item_position = AsyncMock(return_value=1)
        self.async_add_item = AsyncMock(return_value=True)
        self.async_list_item_summary_ids = AsyncMock(return_value=[])
        self.async_move_items = AsyncMock(return_value=[])


@pytest.fixture
def fake_collection_repo() -> _FakeCollectionRepository:
    repo = _FakeCollectionRepository()
    previous = collection_service_module._repo_factory_holder[0]
    CollectionService.configure(lambda: repo)
    try:
        yield repo
    finally:
        collection_service_module._repo_factory_holder[0] = previous


@pytest.mark.asyncio
async def test_add_item_rejects_summary_not_owned_by_actor(
    fake_collection_repo: _FakeCollectionRepository,
) -> None:
    fake_collection_repo.async_summary_belongs_to_user.return_value = False

    with pytest.raises(ResourceNotFoundError):
        await CollectionService.add_item(collection_id=10, summary_id=55, user_id=1001)

    fake_collection_repo.async_summary_belongs_to_user.assert_awaited_once_with(55, 1001)
    fake_collection_repo.async_add_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_items_passes_only_ids_present_in_source_collection(
    fake_collection_repo: _FakeCollectionRepository,
) -> None:
    fake_collection_repo.async_list_item_summary_ids.return_value = [11]
    fake_collection_repo.async_move_items.return_value = [11]

    moved = await CollectionService.move_items(
        source_collection_id=10,
        user_id=1001,
        summary_ids=[11, 12],
        target_collection_id=20,
        position=1,
    )

    assert moved == [11]
    fake_collection_repo.async_list_item_summary_ids.assert_awaited_once_with(10, [11, 12])
    fake_collection_repo.async_move_items.assert_awaited_once_with(10, 20, [11], 1)


@pytest.mark.asyncio
async def test_move_items_returns_empty_when_no_ids_are_in_source_collection(
    fake_collection_repo: _FakeCollectionRepository,
) -> None:
    fake_collection_repo.async_list_item_summary_ids.return_value = []

    moved = await CollectionService.move_items(
        source_collection_id=10,
        user_id=1001,
        summary_ids=[12],
        target_collection_id=20,
        position=1,
    )

    assert moved == []
    fake_collection_repo.async_move_items.assert_not_awaited()
