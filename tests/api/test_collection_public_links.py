from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from app.api.exceptions import RateLimitExceededError, ResourceNotFoundError
from app.api.routers.collections import _build_public_collection_response
from app.api.services import collection_service as collection_service_module
from app.api.services.collection_service import CollectionService
from app.core.time_utils import UTC


class FakeCollectionRepository:
    def __init__(self) -> None:
        self.collection = {
            "id": 10,
            "user_id": 123,
            "name": "Reading List",
            "description": "Shared reads",
            "is_deleted": False,
        }
        self.links: dict[str, dict[str, Any]] = {}
        self.payloads: dict[str, dict[str, Any]] = {}

    async def async_get_collection(self, collection_id: int) -> dict[str, Any] | None:
        return self.collection if collection_id == self.collection["id"] else None

    async def async_get_role(self, collection_id: int, user_id: int) -> str | None:
        if collection_id == self.collection["id"] and user_id == self.collection["user_id"]:
            return "owner"
        return None

    async def async_create_public_link(
        self,
        *,
        collection_id: int,
        token: str,
        expires_at: dt.datetime | None,
        password_hash: str | None,
    ) -> dict[str, Any]:
        link = {
            "id": len(self.links) + 1,
            "token": token,
            "collection_id": collection_id,
            "collection": collection_id,
            "created_at": dt.datetime(2026, 6, 19, 12, 0, tzinfo=UTC),
            "expires_at": expires_at,
            "revoked_at": None,
            "password_hash": password_hash,
            "has_password": password_hash is not None,
            "view_count": 0,
        }
        self.links[token] = link
        self.payloads[token] = {
            "link": link,
            "collection": self.collection,
            "owner": {"display_name": "Owner"},
            "items": [
                {
                    "summary_id": 77,
                    "title": "Article",
                    "url": "https://example.com/a",
                    "summary_250": "Short summary",
                    "tldr": "TLDR",
                    "created_at": dt.datetime(2026, 6, 19, 12, 5, tzinfo=UTC),
                }
            ],
        }
        public = dict(link)
        public.pop("password_hash", None)
        return public

    async def async_list_public_links(self, collection_id: int) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in link.items() if key != "password_hash"}
            for link in self.links.values()
            if link["collection_id"] == collection_id
        ]

    async def async_revoke_public_link(self, collection_id: int, token: str) -> bool:
        link = self.links.get(token)
        if not link or link["collection_id"] != collection_id or link["revoked_at"] is not None:
            return False
        link["revoked_at"] = dt.datetime(2026, 6, 19, 12, 10, tzinfo=UTC)
        return True

    async def async_get_public_link_by_token(
        self, token: str, *, include_password_hash: bool = False
    ) -> dict[str, Any] | None:
        link = self.links.get(token)
        if link is None:
            return None
        public = dict(link)
        if not include_password_hash:
            public.pop("password_hash", None)
        return public

    async def async_get_public_collection_payload(
        self, token: str, *, viewer_ip: str | None
    ) -> dict[str, Any] | None:
        payload = self.payloads.get(token)
        if payload is None:
            return None
        payload["link"]["view_count"] += 1
        return payload


@pytest.fixture(autouse=True)
def clear_public_link_rate_cache() -> None:
    collection_service_module._PUBLIC_LINK_READS.clear()


def _service(repo: FakeCollectionRepository) -> CollectionService:
    return CollectionService(lambda: repo)


@pytest.mark.asyncio
async def test_owner_can_create_list_and_revoke_public_link() -> None:
    repo = FakeCollectionRepository()
    service = _service(repo)

    link = await service.create_public_link(
        collection_id=10,
        user_id=123,
        expires_at=None,
        password=None,
    )
    links = await service.list_public_links(collection_id=10, user_id=123)
    await service.revoke_public_link(collection_id=10, token=link["token"], user_id=123)

    assert link["token"]
    assert links[0]["token"] == link["token"]
    assert repo.links[link["token"]]["revoked_at"] is not None


@pytest.mark.asyncio
async def test_public_link_returns_payload_and_increments_view_count() -> None:
    repo = FakeCollectionRepository()
    service = _service(repo)
    link = await service.create_public_link(
        collection_id=10,
        user_id=123,
        expires_at=None,
        password=None,
    )

    payload = await service.get_public_collection(
        token=link["token"],
        password=None,
        viewer_ip="203.0.113.10",
    )
    response = _build_public_collection_response(payload)

    assert response.name == "Reading List"
    assert response.owner_display_name == "Owner"
    assert response.items[0].summary_id == 77
    assert response.view_count == 1


@pytest.mark.asyncio
async def test_public_link_unknown_revoked_expired_and_wrong_password_return_not_found() -> None:
    repo = FakeCollectionRepository()
    service = _service(repo)
    password_link = await service.create_public_link(
        collection_id=10,
        user_id=123,
        expires_at=None,
        password="secret",
    )
    expired_link = await service.create_public_link(
        collection_id=10,
        user_id=123,
        expires_at=dt.datetime(2020, 1, 1, tzinfo=UTC),
        password=None,
    )
    revoked_link = await service.create_public_link(
        collection_id=10,
        user_id=123,
        expires_at=None,
        password=None,
    )
    await service.revoke_public_link(collection_id=10, token=revoked_link["token"], user_id=123)

    for token, password in (
        ("missing", None),
        (revoked_link["token"], None),
        (expired_link["token"], None),
        (password_link["token"], "wrong"),
    ):
        with pytest.raises(ResourceNotFoundError):
            await service.get_public_collection(
                token=token,
                password=password,
                viewer_ip="203.0.113.20",
            )


@pytest.mark.asyncio
async def test_public_link_password_and_rate_limit() -> None:
    repo = FakeCollectionRepository()
    service = _service(repo)
    link = await service.create_public_link(
        collection_id=10,
        user_id=123,
        expires_at=None,
        password="secret",
    )

    first = await service.get_public_collection(
        token=link["token"],
        password="secret",
        viewer_ip="203.0.113.30",
    )
    with pytest.raises(RateLimitExceededError):
        await service.get_public_collection(
            token=link["token"],
            password="secret",
            viewer_ip="203.0.113.30",
        )

    assert first["link"]["view_count"] == 1
