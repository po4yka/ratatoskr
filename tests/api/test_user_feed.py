from __future__ import annotations

import datetime as dt
from xml.etree import ElementTree

import pytest
from starlette.requests import Request

from app.api.routers.user import feed
from app.core.time_utils import UTC


class FakeAuthRepository:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        self.updated: list[tuple[int, dict[str, object]]] = []

    async def async_replace_active_client_secret(
        self,
        *,
        user_id: int,
        client_id: str,
        secret_hash: str,
        secret_salt: str,
        status: str = "active",
        label: str | None = None,
        description: str | None = None,
        expires_at: dt.datetime | None = None,
    ) -> int:
        for record in self.records:
            if (
                record["user_id"] == user_id
                and record["client_id"] == client_id
                and record["status"] == "active"
            ):
                record["status"] = "revoked"
        record_id = len(self.records) + 1
        self.records.append(
            {
                "id": record_id,
                "user_id": user_id,
                "client_id": client_id,
                "secret_hash": secret_hash,
                "secret_salt": secret_salt,
                "status": status,
                "label": label,
                "description": description,
                "expires_at": expires_at,
            }
        )
        return record_id

    async def async_get_client_secret(
        self, user_id: int, client_id: str
    ) -> dict[str, object] | None:
        matches = [
            record
            for record in self.records
            if record["user_id"] == user_id and record["client_id"] == client_id
        ]
        return matches[-1] if matches else None

    async def async_list_client_secrets(
        self,
        *,
        user_id: int | None = None,
        client_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, object]]:
        rows = self.records
        if user_id is not None:
            rows = [record for record in rows if record["user_id"] == user_id]
        if client_id is not None:
            rows = [record for record in rows if record["client_id"] == client_id]
        if status is not None:
            rows = [record for record in rows if record["status"] == status]
        return rows

    async def async_update_client_secret(self, key_id: int, **fields: object) -> None:
        self.updated.append((key_id, fields))
        for record in self.records:
            if record["id"] == key_id:
                record.update(fields)


class FakeRequestForRotate:
    def url_for(self, name: str) -> str:
        assert name == "get_current_user_library_feed"
        return "https://ratatoskr.test/v1/users/me/feed.xml"


def _request(*, if_none_match: str | None = None) -> Request:
    headers = []
    if if_none_match is not None:
        headers.append((b"if-none-match", if_none_match.encode("utf-8")))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "server": ("ratatoskr.test", 443),
            "path": "/v1/users/me/feed.xml",
            "query_string": b"token=feed-token",
            "headers": headers,
        }
    )


def _item() -> feed.FeedSummary:
    timestamp = dt.datetime(2026, 6, 19, 12, 0, tzinfo=UTC)
    return feed.FeedSummary(
        id=42,
        title="Useful article",
        url="https://example.com/article",
        text="A concise summary.",
        lang="en",
        tags=("ai", "research"),
        published_at=timestamp,
        updated_at=timestamp,
    )


def test_atom_feed_is_well_formed_atom() -> None:
    xml = feed.build_atom_feed(
        user_id=123,
        items=[_item()],
        self_url="https://ratatoskr.test/v1/users/me/feed.xml?token=feed-token",
    )

    root = ElementTree.fromstring(xml)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    assert root.tag == "{http://www.w3.org/2005/Atom}feed"
    assert root.findtext("atom:title", namespaces=ns) == "Ratatoskr saved summaries"
    entry = root.find("atom:entry", namespaces=ns)
    assert entry is not None
    assert entry.findtext("atom:title", namespaces=ns) == "Useful article"
    assert entry.find("atom:link", namespaces=ns).attrib["href"] == "https://example.com/article"


@pytest.mark.asyncio
async def test_if_none_match_returns_304(monkeypatch: pytest.MonkeyPatch) -> None:
    item = _item()
    request = _request()
    etag = feed.build_feed_etag(
        feed.build_atom_feed(user_id=123, items=[item], self_url=str(request.url))
    )
    request_with_etag = _request(if_none_match=etag)
    token = "feed-token"
    secret_hash, secret_salt = feed.new_feed_secret(token)
    auth_repo = FakeAuthRepository()
    await auth_repo.async_replace_active_client_secret(
        user_id=123,
        client_id=feed.RSS_FEED_CLIENT_ID,
        secret_hash=secret_hash,
        secret_salt=secret_salt,
    )

    async def fake_load_feed_summaries(*args: object, **kwargs: object) -> list[feed.FeedSummary]:
        return [item]

    monkeypatch.setattr(feed, "load_feed_summaries", fake_load_feed_summaries)

    response = await feed.get_current_user_library_feed(
        request_with_etag,
        token=token,
        auth_repo=auth_repo,
        session_manager=object(),
    )

    assert response.status_code == 304
    assert response.headers["etag"] == etag
    assert response.headers["cache-control"] == feed.RSS_FEED_CACHE_CONTROL
    assert auth_repo.updated == []


@pytest.mark.asyncio
async def test_rotating_feed_token_invalidates_previous_token() -> None:
    auth_repo = FakeAuthRepository()
    request = FakeRequestForRotate()

    first = await feed.rotate_user_library_feed_token(
        request,
        user={"user_id": 123},
        auth_repo=auth_repo,
    )
    old_token = str(first["data"]["token"])
    second = await feed.rotate_user_library_feed_token(
        request,
        user={"user_id": 123},
        auth_repo=auth_repo,
    )
    new_token = str(second["data"]["token"])

    assert old_token != new_token
    assert auth_repo.records[0]["status"] == "revoked"
    assert auth_repo.records[1]["status"] == "active"
    assert await feed._find_active_feed_secret(auth_repo, old_token) is None
    active = await feed._find_active_feed_secret(auth_repo, new_token)
    assert active is not None
    assert active["id"] == 2
