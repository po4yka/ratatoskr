from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from app.api.middleware import _resolve_bucket
from app.api.routers.auth.tokens import create_access_token
from app.config import Config
from app.db.models import User

if TYPE_CHECKING:
    from fastapi.testclient import TestClient


def _headers(user_id: int) -> dict[str, str]:
    token = create_access_token(user_id, client_id="test")
    return {"Authorization": f"Bearer {token}"}


async def _seed(db, *users: tuple[int, str | None, str | None]) -> None:
    """Insert (telegram_user_id, username, display_name) rows."""
    async with db.transaction() as session:
        session.add_all(
            [
                User(telegram_user_id=uid, username=username, display_name=display_name)
                for uid, username, display_name in users
            ]
        )


def _search(client: TestClient, user_id: int, **params: object):
    return client.get("/v1/users/search", params=params, headers=_headers(user_id))


@pytest.mark.asyncio
async def test_search_returns_prefix_matches(client: TestClient, db, user_factory):
    """Catches the query being narrowed to equality or widened to an infix match."""
    user_id = int(Config.get_allowed_user_ids()[0])
    await user_factory(telegram_user_id=user_id, username="caller")
    await _seed(db, (9001, "alice", "Alice A"), (9002, "alicia", None), (9003, "bob", None))

    response = _search(client, user_id, q="ali")

    assert response.status_code == 200
    usernames = {entry["username"] for entry in response.json()["data"]["users"]}
    assert usernames == {"alice", "alicia"}


@pytest.mark.asyncio
async def test_search_is_case_insensitive(client: TestClient, db, user_factory):
    """Catches `ilike` being downgraded to `like`."""
    user_id = int(Config.get_allowed_user_ids()[0])
    await user_factory(telegram_user_id=user_id, username="caller")
    await _seed(db, (9010, "alice", None))

    response = _search(client, user_id, q="AL")

    assert response.status_code == 200
    assert [entry["username"] for entry in response.json()["data"]["users"]] == ["alice"]


@pytest.mark.asyncio
async def test_search_exposes_only_id_username_and_display_name(
    client: TestClient, db, user_factory
):
    """Pins the response shape at the HTTP boundary.

    This holds because `UserSearchResult` declares exactly three fields and Pydantic
    drops the rest -- verified by mutation: replacing the repository's three-column
    select with `select(User)` + `model_to_dict` keeps this test green. So this asserts
    the response model, NOT the query behind it; `test_repository_projection_selects_only_three_columns`
    covers that side, and both are needed to keep `link_nonce`, `preferences_json` and
    the `linked_telegram_*` block away from a stranger.
    """
    user_id = int(Config.get_allowed_user_ids()[0])
    await user_factory(telegram_user_id=user_id, username="caller")
    await _seed(db, (9020, "alice", "Alice A"))

    response = _search(client, user_id, q="alice")

    assert response.status_code == 200
    entries = response.json()["data"]["users"]
    assert entries, "expected the seeded user to be found"
    for entry in entries:
        assert set(entry) == {"userId", "username", "displayName"}


@pytest.mark.asyncio
async def test_search_escapes_like_metacharacters(client: TestClient, db, user_factory):
    """Catches the %/_/backslash escaping being dropped.

    Unescaped, `_` is a single-character wildcard and `%` matches everything, so
    both queries below would start returning rows they must not.
    """
    user_id = int(Config.get_allowed_user_ids()[0])
    await user_factory(telegram_user_id=user_id, username="caller")
    await _seed(db, (9030, "alice", None), (9031, "axbc", None))

    assert _search(client, user_id, q="a_").json()["data"]["users"] == []
    assert _search(client, user_id, q="%a").json()["data"]["users"] == []


@pytest.mark.asyncio
async def test_search_respects_the_limit_bound(client: TestClient, db, user_factory):
    """Catches `.limit()` being dropped from the query."""
    user_id = int(Config.get_allowed_user_ids()[0])
    await user_factory(telegram_user_id=user_id, username="caller")
    await _seed(db, *[(9100 + n, f"user{n}", None) for n in range(5)])

    response = _search(client, user_id, q="user", limit=2)

    assert response.status_code == 200
    assert len(response.json()["data"]["users"]) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{}, {"q": ""}, {"q": "a"}, {"q": "ab", "limit": 0}])
async def test_search_rejects_out_of_bounds_input(
    client: TestClient, user_factory, params: dict[str, object]
):
    """Catches `min_length=2` being removed, which turns this into "list all users"."""
    user_id = int(Config.get_allowed_user_ids()[0])
    await user_factory(telegram_user_id=user_id, username="caller")

    assert _search(client, user_id, **params).status_code == 422


@pytest.mark.asyncio
async def test_search_requires_authentication(client: TestClient) -> None:
    """This repo has no router-level auth, so the endpoint is one deleted line from public."""
    assert client.get("/v1/users/search", params={"q": "ali"}).status_code == 401


@pytest.mark.asyncio
async def test_repository_projection_selects_only_three_columns(db, user_factory) -> None:
    """Catches the query widening to a whole-row select.

    The API-level test cannot see this: `UserSearchResult` silently drops any extra
    keys, so a `select(User)` regression reaches the model and disappears there --
    confirmed by mutation. Asserting the repository's own dict keys is the only place
    that failure surfaces, and it matters because a whole-row projection pulls
    `link_nonce` and the `linked_telegram_*` block into application memory on a request
    made by a different user.
    """
    from app.infrastructure.persistence.repositories.user_repository import (
        UserRepositoryAdapter,
    )

    await user_factory(telegram_user_id=int(Config.get_allowed_user_ids()[0]), username="caller")
    await _seed(db, (9200, "alice", "Alice A"))

    matches = await UserRepositoryAdapter(db).async_search_users_by_username_prefix(
        prefix="alice", limit=10
    )

    assert matches, "expected the seeded user to be found"
    for match in matches:
        assert set(match) == {"user_id", "username", "display_name"}


def test_users_search_path_resolves_to_the_search_rate_limit_bucket() -> None:
    """Pins the accidental coupling the endpoint depends on for its rate limit.

    `rate_limit_middleware` has no opt-in: the bucket comes from the path, and only
    the `/search` substring branch keeps this route at 50/60s instead of the 100/60s
    default. Renaming the route would relax the limit with nothing else failing.
    """
    assert _resolve_bucket("GET", "/v1/users/search") == "search"
