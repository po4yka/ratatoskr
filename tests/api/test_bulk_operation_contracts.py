from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.api.exceptions import ResourceNotFoundError, ValidationError
from app.api.models.requests import (
    AttachTagsRequest,
    CollectionItemMoveRequest,
    CollectionItemReorderRequest,
    CollectionReorderRequest,
    MergeTagsRequest,
)
from app.api.routers import collections
from app.api.routers.content import summaries
from app.api.routers.user import tags
from app.api.services.collection_service import CollectionService
from app.core.time_utils import UTC
from app.db.models import CollectionItem


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
            items=[{"summary_id": item, "position": item} for item in oversized]
        )
    with pytest.raises(PydanticValidationError):
        CollectionReorderRequest(
            items=[{"collection_id": item, "position": item} for item in oversized]
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


@pytest.mark.asyncio
async def test_collection_move_items_skips_absent_ids_and_dedupes_response(
    db, user_factory, summary_factory
) -> None:
    owner = await user_factory(username="bulk-move-owner", telegram_user_id=8101)
    source = await CollectionService.create_collection(
        user_id=owner.telegram_user_id,
        name="Source",
        description=None,
        parent_id=None,
        position=None,
    )
    target = await CollectionService.create_collection(
        user_id=owner.telegram_user_id,
        name="Target",
        description=None,
        parent_id=None,
        position=None,
    )
    owned = await summary_factory(user=owner)
    absent = await summary_factory(user=owner)
    await CollectionService.add_item(source["id"], owned.id, owner.telegram_user_id)

    response = await collections.move_collection_items(
        collection_id=source["id"],
        body=CollectionItemMoveRequest(
            summary_ids=[owned.id, owned.id, absent.id], target_collection_id=target["id"]
        ),
        user={"user_id": owner.telegram_user_id},
    )

    assert response["data"] == {"movedSummaryIds": [owned.id]}
    assert (
        not CollectionItem.select()
        .where(
            (CollectionItem.collection_id == target["id"])
            & (CollectionItem.summary_id == absent.id)
        )
        .exists()
    )


@pytest.mark.asyncio
async def test_collection_reorder_items_errors_on_ids_not_in_owned_collection(
    db, user_factory, summary_factory
) -> None:
    owner = await user_factory(username="bulk-reorder-owner", telegram_user_id=8201)
    other = await user_factory(username="bulk-reorder-other", telegram_user_id=8202)
    collection = await CollectionService.create_collection(
        user_id=owner.telegram_user_id,
        name="Owned",
        description=None,
        parent_id=None,
        position=None,
    )
    owned = await summary_factory(user=owner)
    other_summary = await summary_factory(user=other)
    await CollectionService.add_item(collection["id"], owned.id, owner.telegram_user_id)

    with pytest.raises(ResourceNotFoundError):
        await collections.reorder_collection_items(
            collection_id=collection["id"],
            body=CollectionItemReorderRequest(
                items=[
                    {"summary_id": owned.id, "position": 1},
                    {"summary_id": other_summary.id, "position": 2},
                ]
            ),
            user={"user_id": owner.telegram_user_id},
        )


class _TagRepo:
    def __init__(self, tags_by_id: dict[int, dict[str, Any]]) -> None:
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
        self.async_get_tags_for_summary = AsyncMock(return_value=[])
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
async def test_attach_tags_dedupes_ids_and_rejects_cross_user_tags(
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

    repo.async_attach_tag.assert_awaited_once_with(99, 1, source="manual")


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
