from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api.exceptions import AuthorizationError, ResourceNotFoundError, ValidationError
from app.api.models.requests import (
    AttachTagsRequest,
    CollectionItemMoveRequest,
    CollectionItemReorderItem,
    CollectionItemReorderRequest,
    CollectionReorderItem,
    CollectionReorderRequest,
    MergeTagsRequest,
)
from app.api.routers import collections
from app.api.routers.content import summaries
from app.api.routers.user import tags
from app.api.services.collection_service import CollectionService
from app.core.time_utils import UTC


def test_bulk_request_models_reject_oversized_batches() -> None:
    oversized = list(range(1, 502))

    with pytest.raises(PydanticValidationError):
        summaries._BulkMarkReadRequest(summary_ids=oversized)
    with pytest.raises(PydanticValidationError):
        summaries._BulkFavoriteRequest(summary_ids=oversized)
    with pytest.raises(PydanticValidationError):
        summaries._BulkDeleteRequest(summary_ids=oversized)
    with pytest.raises(PydanticValidationError):
        CollectionItemMoveRequest(summary_ids=oversized, target_collection_id=1)
    with pytest.raises(PydanticValidationError):
        CollectionItemReorderRequest(
            items=[CollectionItemReorderItem(summary_id=item, position=item) for item in oversized]
        )
    with pytest.raises(PydanticValidationError):
        CollectionReorderRequest(
            items=[CollectionReorderItem(collection_id=item, position=item) for item in oversized]
        )
    with pytest.raises(PydanticValidationError):
        AttachTagsRequest(tag_ids=oversized)
    with pytest.raises(PydanticValidationError):
        MergeTagsRequest(source_tag_ids=oversized, target_tag_id=1)


@pytest.mark.asyncio
async def test_summary_bulk_router_response_shapes_and_duplicate_ids() -> None:
    use_case = AsyncMock()
    use_case.bulk_mark_as_read = AsyncMock(return_value=2)
    use_case.bulk_set_favorite = AsyncMock(return_value=2)
    use_case.bulk_delete = AsyncMock(return_value=2)
    user = {"user_id": 8001}

    mark_response = await summaries.bulk_mark_read(
        body=summaries._BulkMarkReadRequest(summary_ids=[1, 2, 1]),
        user=user,
        use_case=use_case,
    )
    favorite_response = await summaries.bulk_favorite(
        body=summaries._BulkFavoriteRequest(summary_ids=[1, 2, 1], value=True),
        user=user,
        use_case=use_case,
    )
    delete_response = await summaries.bulk_delete(
        body=summaries._BulkDeleteRequest(summary_ids=[1, 2, 1]),
        user=user,
        use_case=use_case,
    )

    assert mark_response["data"] == {"updated": 2}
    assert favorite_response["data"] == {"updated": 2}
    assert delete_response["data"] == {"updated": 2}
    use_case.bulk_mark_as_read.assert_awaited_once_with(user_id=8001, summary_ids=[1, 2, 1])
    use_case.bulk_set_favorite.assert_awaited_once_with(
        user_id=8001, summary_ids=[1, 2, 1], value=True
    )
    use_case.bulk_delete.assert_awaited_once_with(user_id=8001, summary_ids=[1, 2, 1])


class _CollectionRepo:
    def __init__(self) -> None:
        self.collections = {
            10: {"id": 10, "user_id": 8101, "parent_id": None, "collection_type": "manual"},
            11: {"id": 11, "user_id": 8101, "parent_id": None, "collection_type": "manual"},
            20: {"id": 20, "user_id": 8101, "parent_id": 10, "collection_type": "manual"},
            21: {"id": 21, "user_id": 8101, "parent_id": 10, "collection_type": "manual"},
            30: {"id": 30, "user_id": 8201, "parent_id": 10, "collection_type": "manual"},
        }
        self.roles = {
            (10, 8101): "owner",
            (11, 8101): "owner",
            (20, 8101): "owner",
            (21, 8101): "owner",
        }
        self.source_items = {10: {101, 102}}
        self.async_reorder_items = AsyncMock(return_value=None)
        self.async_reorder_collections = AsyncMock(return_value=None)
        self.async_move_items = AsyncMock(side_effect=lambda _source, _target, ids, _position: ids)

    async def async_get_collection(self, collection_id: int) -> dict[str, Any] | None:
        return self.collections.get(collection_id)

    async def async_get_role(self, collection_id: int, user_id: int) -> str | None:
        return self.roles.get((collection_id, user_id))

    async def async_list_item_summary_ids(
        self, collection_id: int, summary_ids: list[int]
    ) -> list[int]:
        existing = self.source_items.get(collection_id, set())
        return [summary_id for summary_id in summary_ids if summary_id in existing]


def _collection_service(repo: _CollectionRepo) -> CollectionService:
    return CollectionService(lambda: repo)


@pytest.mark.asyncio
async def test_collection_move_items_skips_absent_ids_and_dedupes_response() -> None:
    repo = _CollectionRepo()
    service = _collection_service(repo)

    response = await collections.move_collection_items(
        collection_id=10,
        body=CollectionItemMoveRequest(summary_ids=[101, 101, 999], target_collection_id=11),
        user={"user_id": 8101},
        service=service,
    )

    assert response["data"] == {"movedSummaryIds": [101]}
    repo.async_move_items.assert_awaited_once_with(10, 11, [101], None)


@pytest.mark.asyncio
async def test_collection_move_items_skips_all_ids_absent_from_source() -> None:
    repo = _CollectionRepo()
    service = _collection_service(repo)

    response = await collections.move_collection_items(
        collection_id=10,
        body=CollectionItemMoveRequest(summary_ids=[999, 1000], target_collection_id=11),
        user={"user_id": 8101},
        service=service,
    )

    assert response["data"] == {"movedSummaryIds": []}
    repo.async_move_items.assert_not_awaited()


@pytest.mark.asyncio
async def test_collection_reorder_items_errors_on_ids_not_in_owned_collection() -> None:
    repo = _CollectionRepo()
    service = _collection_service(repo)

    with pytest.raises(ResourceNotFoundError):
        await collections.reorder_collection_items(
            collection_id=10,
            body=CollectionItemReorderRequest(
                items=[
                    CollectionItemReorderItem(summary_id=101, position=1),
                    CollectionItemReorderItem(summary_id=999, position=2),
                ]
            ),
            user={"user_id": 8101},
            service=service,
        )

    repo.async_reorder_items.assert_not_awaited()


@pytest.mark.asyncio
async def test_collection_reorder_items_dedupes_owned_ids() -> None:
    repo = _CollectionRepo()
    service = _collection_service(repo)

    await collections.reorder_collection_items(
        collection_id=10,
        body=CollectionItemReorderRequest(
            items=[
                CollectionItemReorderItem(summary_id=101, position=1),
                CollectionItemReorderItem(summary_id=101, position=2),
            ]
        ),
        user={"user_id": 8101},
        service=service,
    )

    repo.async_reorder_items.assert_awaited_once_with(10, [{"summary_id": 101, "position": 1}])


@pytest.mark.asyncio
async def test_collection_reorder_collections_rejects_cross_user_ids() -> None:
    repo = _CollectionRepo()
    service = _collection_service(repo)

    with pytest.raises(AuthorizationError):
        await collections.reorder_collections(
            collection_id=10,
            body=CollectionReorderRequest(
                items=[
                    CollectionReorderItem(collection_id=20, position=1),
                    CollectionReorderItem(collection_id=30, position=2),
                ]
            ),
            user={"user_id": 8101},
            service=service,
        )

    repo.async_reorder_collections.assert_not_awaited()


@pytest.mark.asyncio
async def test_collection_reorder_collections_dedupes_owned_ids() -> None:
    repo = _CollectionRepo()
    service = _collection_service(repo)

    await collections.reorder_collections(
        collection_id=10,
        body=CollectionReorderRequest(
            items=[
                CollectionReorderItem(collection_id=20, position=1),
                CollectionReorderItem(collection_id=20, position=2),
            ]
        ),
        user={"user_id": 8101},
        service=service,
    )

    repo.async_reorder_collections.assert_awaited_once_with(
        10, [{"collection_id": 20, "position": 1}]
    )


class _TagRepo:
    def __init__(
        self,
        tags_by_id: dict[int, dict[str, Any]],
        *,
        tags_for_summary: list[dict[str, Any]] | None = None,
    ) -> None:
        self.tags_by_id = tags_by_id
        self.async_get_tag_by_id = AsyncMock(side_effect=lambda tag_id: self.tags_by_id.get(tag_id))
        self.async_merge_tags = AsyncMock(return_value=None)
        self.async_attach_tag = AsyncMock(
            side_effect=lambda summary_id, tag_id, source: {
                "summary_id": summary_id,
                "tag_id": tag_id,
                "source": source,
            }
        )
        self.async_get_tags_for_summary = AsyncMock(return_value=tags_for_summary or [])
        self.async_get_tag_by_normalized_name = AsyncMock(return_value=None)
        self.async_create_tag = AsyncMock()


def _tag(tag_id: int, user_id: int, name: str) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "id": tag_id,
        "user_id": user_id,
        "name": name,
        "color": None,
        "summary_count": 0,
        "created_at": now,
        "updated_at": now,
        "is_deleted": False,
    }


@pytest.mark.asyncio
async def test_tag_merge_rejects_cross_user_source_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _TagRepo(
        {
            1: _tag(1, 8301, "target"),
            2: _tag(2, 8301, "owned-source"),
            3: _tag(3, 8302, "other-source"),
        }
    )
    monkeypatch.setattr(tags, "_get_tag_repo", lambda: repo)

    with pytest.raises(ResourceNotFoundError):
        await tags.merge_tags(
            body=MergeTagsRequest(source_tag_ids=[2, 3], target_tag_id=1),
            user={"user_id": 8301},
        )

    repo.async_merge_tags.assert_not_awaited()


@pytest.mark.asyncio
async def test_tag_merge_rejects_all_cross_user_sources_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _TagRepo({1: _tag(1, 8301, "target"), 2: _tag(2, 8302, "other")})
    monkeypatch.setattr(tags, "_get_tag_repo", lambda: repo)

    with pytest.raises(ResourceNotFoundError):
        await tags.merge_tags(
            body=MergeTagsRequest(source_tag_ids=[2], target_tag_id=1),
            user={"user_id": 8301},
        )

    repo.async_merge_tags.assert_not_awaited()


@pytest.mark.asyncio
async def test_tag_merge_dedupes_source_ids_and_pins_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _TagRepo({1: _tag(1, 8401, "target"), 2: _tag(2, 8401, "source")})
    monkeypatch.setattr(tags, "_get_tag_repo", lambda: repo)

    response = await tags.merge_tags(
        body=MergeTagsRequest(source_tag_ids=[2, 2], target_tag_id=1),
        user={"user_id": 8401},
    )

    assert response["data"] == {"merged": True, "target_tag_id": 1}
    repo.async_merge_tags.assert_awaited_once_with([2], 1)


@pytest.mark.asyncio
async def test_attach_tags_dedupes_ids_and_pins_response_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tag = _tag(1, 8501, "owned")
    repo = _TagRepo({1: tag}, tags_for_summary=[tag])
    monkeypatch.setattr(tags, "_get_tag_repo", lambda: repo)
    monkeypatch.setattr(tags, "_ensure_summary_owned", AsyncMock(return_value=None))

    response = await tags.attach_tags(
        summary_id=99,
        body=AttachTagsRequest(tag_ids=[1, 1]),
        user={"user_id": 8501},
    )

    assert response["data"]["tags"][0]["id"] == 1
    repo.async_attach_tag.assert_awaited_once_with(99, 1, source="manual")


@pytest.mark.asyncio
async def test_attach_tags_rejects_mixed_cross_user_tags_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _TagRepo({1: _tag(1, 8501, "owned"), 2: _tag(2, 8502, "other")})
    monkeypatch.setattr(tags, "_get_tag_repo", lambda: repo)
    monkeypatch.setattr(tags, "_ensure_summary_owned", AsyncMock(return_value=None))

    with pytest.raises(ResourceNotFoundError):
        await tags.attach_tags(
            summary_id=99,
            body=AttachTagsRequest(tag_ids=[1, 1, 2]),
            user={"user_id": 8501},
        )

    repo.async_attach_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_tags_rejects_all_cross_user_tags_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _TagRepo({2: _tag(2, 8502, "other")})
    monkeypatch.setattr(tags, "_get_tag_repo", lambda: repo)
    monkeypatch.setattr(tags, "_ensure_summary_owned", AsyncMock(return_value=None))

    with pytest.raises(ResourceNotFoundError):
        await tags.attach_tags(
            summary_id=99,
            body=AttachTagsRequest(tag_ids=[2]),
            user={"user_id": 8501},
        )

    repo.async_attach_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_tags_requires_non_empty_deduped_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _TagRepo({})
    monkeypatch.setattr(tags, "_get_tag_repo", lambda: repo)
    monkeypatch.setattr(tags, "_ensure_summary_owned", AsyncMock(return_value=None))

    with pytest.raises(ValidationError):
        await tags.attach_tags(
            summary_id=99,
            body=AttachTagsRequest(),
            user={"user_id": 8601},
        )
