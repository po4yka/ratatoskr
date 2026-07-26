from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.api.exceptions import ResourceNotFoundError
from app.api.routers.user import tags


class _FakeTagRepository:
    def __init__(self, tag: dict | None) -> None:
        self.async_get_tag_by_id = AsyncMock(return_value=tag)
        self.async_detach_tag = AsyncMock(return_value=None)


@pytest.mark.asyncio
async def test_detach_tag_rejects_tag_owned_by_another_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeTagRepository(tag={"id": 44, "user": 2002, "is_deleted": False})
    monkeypatch.setattr(tags, "_get_tag_repo", lambda: repo)
    monkeypatch.setattr(tags, "_ensure_summary_owned", AsyncMock(return_value=None))

    with pytest.raises(ResourceNotFoundError):
        await tags.detach_tag(summary_id=10, tag_id=44, user={"user_id": 1001})

    repo.async_get_tag_by_id.assert_awaited_once_with(44)
    repo.async_detach_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_detach_tag_allows_owned_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _FakeTagRepository(tag={"id": 44, "user": 1001, "is_deleted": False})
    monkeypatch.setattr(tags, "_get_tag_repo", lambda: repo)
    monkeypatch.setattr(tags, "_ensure_summary_owned", AsyncMock(return_value=None))

    response = await tags.detach_tag(summary_id=10, tag_id=44, user={"user_id": 1001})

    repo.async_get_tag_by_id.assert_awaited_once_with(44)
    repo.async_detach_tag.assert_awaited_once_with(10, 44)
    assert response["success"] is True


@pytest.mark.asyncio
async def test_list_summary_tags_requires_owned_summary_and_returns_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _FakeTagRepository(tag=None)
    repo.async_get_tags_for_summary = AsyncMock(
        return_value=[
            {
                "id": 44,
                "name": "python",
                "color": "#3776ab",
                "summary_count": 2,
                "created_at": None,
                "updated_at": None,
            }
        ]
    )
    monkeypatch.setattr(tags, "_get_tag_repo", lambda: repo)
    ensure_owned = AsyncMock(return_value=None)
    monkeypatch.setattr(tags, "_ensure_summary_owned", ensure_owned)

    response = await tags.list_summary_tags(summary_id=10, user={"user_id": 1001})

    assert response["data"]["tags"][0]["name"] == "python"
    ensure_owned.assert_awaited_once_with(10, 1001)
    repo.async_get_tags_for_summary.assert_awaited_once_with(10)
