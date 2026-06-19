"""Tests for saved searches and opt-in search history."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select

from app.api.routers.content.search import SavedSearchCreateRequest
from app.db.models import SearchHistoryEntry, User


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_saved_search_request_accepts_camel_case_filter_aliases() -> None:
    body = SavedSearchCreateRequest.model_validate(
        {
            "name": "Morning Rust",
            "query": "tag:rust",
            "minSimilarity": 0.55,
            "startDate": "2026-01-01",
            "endDate": "2026-06-19",
            "isRead": False,
            "isFavorited": True,
        }
    )

    assert body.min_similarity == 0.55
    assert body.start_date == "2026-01-01"
    assert body.end_date == "2026-06-19"
    assert body.is_read is False
    assert body.is_favorited is True


def test_saved_search_crud_round_trips(client: Any, search_token: str) -> None:
    create = client.post(
        "/v1/searches/saved",
        json={
            "name": "Rust hybrid",
            "query": "tag:rust",
            "mode": "hybrid",
            "language": "en",
            "tags": ["#rust"],
            "minSimilarity": 0.42,
        },
        headers=_auth(search_token),
    )
    assert create.status_code == 201
    created = create.json()["data"]
    assert created["name"] == "Rust hybrid"
    assert created["query"] == "tag:rust"
    assert created["filters"]["mode"] == "hybrid"
    assert created["filters"]["min_similarity"] == 0.42

    listed = client.get("/v1/searches/saved", headers=_auth(search_token))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["data"]["saved_searches"]] == [created["id"]]

    deleted = client.delete(f"/v1/searches/saved/{created['id']}", headers=_auth(search_token))
    assert deleted.status_code == 204

    listed_again = client.get("/v1/searches/saved", headers=_auth(search_token))
    assert listed_again.status_code == 200
    assert listed_again.json()["data"]["saved_searches"] == []


def test_run_saved_search_uses_same_params_as_direct_search(
    client: Any,
    search_token: str,
    mock_search_service_results: Any,
) -> None:
    create = client.post(
        "/v1/searches/saved",
        json={
            "name": "AI reads",
            "query": "artificial intelligence",
            "mode": "keyword",
            "limit": 7,
            "offset": 3,
            "language": "en",
            "isFavorited": True,
        },
        headers=_auth(search_token),
    )
    saved_id = create.json()["data"]["id"]

    direct = client.get(
        "/v1/search",
        params={
            "q": "artificial intelligence",
            "mode": "keyword",
            "limit": 7,
            "offset": 3,
            "language": "en",
            "is_favorited": True,
        },
        headers=_auth(search_token),
    )
    run = client.post(f"/v1/searches/saved/{saved_id}/run", headers=_auth(search_token))

    assert direct.status_code == 200
    assert run.status_code == 200
    assert run.json()["data"] == direct.json()["data"]
    direct_call, run_call = mock_search_service_results.await_args_list[-2:]
    assert run_call.kwargs["q"] == direct_call.kwargs["q"]
    assert run_call.kwargs["mode"] == direct_call.kwargs["mode"]
    assert run_call.kwargs["limit"] == direct_call.kwargs["limit"]
    assert run_call.kwargs["offset"] == direct_call.kwargs["offset"]
    assert run_call.kwargs["filters"].language == direct_call.kwargs["filters"].language
    assert run_call.kwargs["filters"].is_favorited is True


@pytest.mark.asyncio
async def test_search_history_is_opt_in_and_clearable(
    client: Any,
    db: Any,
    search_user: User,
    search_token: str,
    mock_search_service_results: Any,
) -> None:
    off_response = client.get(
        "/v1/search",
        params={"q": "history off"},
        headers=_auth(search_token),
    )
    assert off_response.status_code == 200

    async with db.session() as session:
        off_count = len(
            (
                await session.execute(
                    select(SearchHistoryEntry).where(
                        SearchHistoryEntry.user_id == search_user.telegram_user_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert off_count == 0

    async with db.transaction() as session:
        user = await session.get(User, search_user.telegram_user_id)
        assert user is not None
        user.preferences_json = {"search_history_enabled": True}

    on_response = client.get(
        "/v1/search",
        params={"q": "history on", "mode": "hybrid", "limit": 5},
        headers=_auth(search_token),
    )
    assert on_response.status_code == 200

    history = client.get("/v1/searches/history", headers=_auth(search_token))
    assert history.status_code == 200
    data = history.json()["data"]
    assert data["enabled"] is True
    assert len(data["entries"]) == 1
    assert data["entries"][0]["query"] == "history on"
    assert data["entries"][0]["filters"]["mode"] == "hybrid"

    cleared = client.delete("/v1/searches/history", headers=_auth(search_token))
    assert cleared.status_code == 200
    assert cleared.json()["data"] == {"cleared": True}

    empty = client.get("/v1/searches/history", headers=_auth(search_token))
    assert empty.status_code == 200
    assert empty.json()["data"]["entries"] == []
