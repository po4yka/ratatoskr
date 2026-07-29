"""Tests for GitHubGraphQLClient — star list reads over GraphQL."""

from __future__ import annotations

import httpx
import pytest

from app.adapters.github.exceptions import (
    GitHubAuthError,
    GitHubRateLimitError,
    GitHubServerError,
)
from app.adapters.github.github_graphql_client import GitHubGraphQLClient


def _edge(
    cursor: str, name: str, ids: list[int], *, has_next: bool = False, end: str | None = None
):
    return {
        "cursor": cursor,
        "node": {
            "name": name,
            "slug": name.lower().replace(" ", "-"),
            "isPrivate": False,
            "items": {
                "pageInfo": {"hasNextPage": has_next, "endCursor": end},
                "nodes": [{"databaseId": i} for i in ids],
            },
        },
    }


def _client_with(handler) -> GitHubGraphQLClient:
    client = GitHubGraphQLClient("ghp_fake")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


@pytest.mark.asyncio
async def test_fetch_star_lists_maps_names_and_database_ids():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "viewer": {
                        "lists": {
                            "edges": [
                                _edge("c1", "Android", [1001, 1002]),
                                _edge("c2", "InfoSec", [1003]),
                            ]
                        }
                    }
                }
            },
        )

    async with _client_with(handler) as client:
        lists = await client.fetch_star_lists()

    assert [entry.name for entry in lists] == ["Android", "InfoSec"]
    assert lists[0].repo_github_ids == [1001, 1002]
    assert lists[1].repo_github_ids == [1003]
    assert lists[0].slug == "android"


@pytest.mark.asyncio
async def test_fetch_star_lists_pages_overflowing_list_items():
    """A list with >1 page is re-selected via the preceding list's cursor."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        calls.append(body["variables"])
        if "listCursor" not in body["variables"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "viewer": {
                            "lists": {
                                "edges": [
                                    _edge("c1", "Small", [1]),
                                    _edge("c2", "Big", [2], has_next=True, end="i1"),
                                ]
                            }
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "viewer": {
                        "lists": {
                            "nodes": [
                                {
                                    "name": "Big",
                                    "items": {
                                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                                        "nodes": [{"databaseId": 3}],
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        )

    async with _client_with(handler) as client:
        lists = await client.fetch_star_lists()

    big = next(entry for entry in lists if entry.name == "Big")
    assert big.repo_github_ids == [2, 3]
    # The follow-up query positions on the cursor of the list *before* "Big".
    assert calls[1]["listCursor"] == "c1"
    assert calls[1]["itemCursor"] == "i1"


@pytest.mark.asyncio
async def test_non_repository_items_are_skipped():
    """A list can hold an entry that is not a readable Repository node."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "viewer": {
                        "lists": {
                            "edges": [
                                {
                                    "cursor": "c1",
                                    "node": {
                                        "name": "Mixed",
                                        "slug": "mixed",
                                        "isPrivate": True,
                                        "items": {
                                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                                            "nodes": [{}, {"databaseId": 7}, {"databaseId": None}],
                                        },
                                    },
                                }
                            ]
                        }
                    }
                }
            },
        )

    async with _client_with(handler) as client:
        lists = await client.fetch_star_lists()

    assert lists[0].repo_github_ids == [7]
    assert lists[0].is_private is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_type", "expected"),
    [
        ("RATE_LIMITED", GitHubRateLimitError),
        ("FORBIDDEN", GitHubAuthError),
        ("INSUFFICIENT_SCOPES", GitHubAuthError),
        ("SOMETHING_ELSE", GitHubServerError),
    ],
)
async def test_graphql_errors_map_to_rest_exception_types(error_type, expected):
    """GraphQL reports failures as HTTP 200 with an errors array."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": None, "errors": [{"type": error_type, "message": "nope"}]},
        )

    async with _client_with(handler) as client:
        with pytest.raises(expected):
            await client.fetch_star_lists()


@pytest.mark.asyncio
async def test_http_401_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    async with _client_with(handler) as client:
        with pytest.raises(GitHubAuthError):
            await client.fetch_star_lists()


@pytest.mark.asyncio
async def test_rate_limited_response_carries_reset_epoch():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1893456000"},
            json={},
        )

    async with _client_with(handler) as client:
        with pytest.raises(GitHubRateLimitError) as exc_info:
            await client.fetch_star_lists()

    assert exc_info.value.reset_epoch == 1893456000


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def _captured(handler_body):
    """Return (client, calls) where calls records each GraphQL variables payload."""
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        calls.append(body["variables"])
        return handler_body(body)

    return _client_with(handler), calls


@pytest.mark.asyncio
async def test_create_star_list_returns_the_created_list():
    client, calls = _captured(
        lambda body: httpx.Response(
            200,
            json={
                "data": {
                    "createUserList": {
                        "list": {
                            "id": "UL_1",
                            "name": "Android",
                            "slug": "android",
                            "description": "mobile",
                            "isPrivate": False,
                        }
                    }
                }
            },
        )
    )

    async with client:
        created = await client.create_star_list("Android", description="mobile")

    assert (created.id, created.name, created.slug) == ("UL_1", "Android", "android")
    assert calls[0] == {"name": "Android", "description": "mobile", "isPrivate": False}


@pytest.mark.asyncio
async def test_update_star_list_passes_only_given_fields_as_values():
    client, calls = _captured(
        lambda body: httpx.Response(
            200,
            json={
                "data": {
                    "updateUserList": {
                        "list": {
                            "id": "UL_1",
                            "name": "Android Core",
                            "slug": "android-core",
                            "description": "",
                            "isPrivate": True,
                        }
                    }
                }
            },
        )
    )

    async with client:
        updated = await client.update_star_list("UL_1", name="Android Core", is_private=True)

    assert updated.name == "Android Core"
    assert updated.is_private is True
    # description was not supplied, so it travels as null and GitHub keeps it.
    assert calls[0]["description"] is None


@pytest.mark.asyncio
async def test_delete_star_list_issues_the_mutation():
    client, calls = _captured(
        lambda body: httpx.Response(
            200, json={"data": {"deleteUserList": {"user": {"login": "x"}}}}
        )
    )

    async with client:
        await client.delete_star_list("UL_9")

    assert calls[0] == {"listId": "UL_9"}


@pytest.mark.asyncio
async def test_set_repository_lists_resolves_node_id_then_overwrites():
    def body_for(body):
        if "owner" in body["variables"]:
            return httpx.Response(200, json={"data": {"repository": {"id": "R_kg1"}}})
        return httpx.Response(
            200,
            json={
                "data": {
                    "updateUserListsForItem": {
                        "lists": [
                            {"id": "UL_1", "name": "Android", "slug": "android"},
                            {"id": "UL_2", "name": "KMP", "slug": "kmp"},
                        ]
                    }
                }
            },
        )

    client, calls = _captured(body_for)

    async with client:
        names = await client.set_repository_lists(
            owner="square", name="metro", list_ids=["UL_1", "UL_2"]
        )

    assert names == ["Android", "KMP"]
    assert calls[0] == {"owner": "square", "name": "metro"}
    assert calls[1] == {"itemId": "R_kg1", "listIds": ["UL_1", "UL_2"]}


@pytest.mark.asyncio
async def test_set_repository_lists_raises_when_repo_is_unknown():
    from app.adapters.github.exceptions import GitHubNotFoundError

    client, _calls = _captured(
        lambda body: httpx.Response(200, json={"data": {"repository": None}})
    )

    async with client:
        with pytest.raises(GitHubNotFoundError):
            await client.set_repository_lists(owner="nobody", name="nothing", list_ids=[])


@pytest.mark.asyncio
async def test_fetch_star_list_summaries_skips_item_paging():
    client, calls = _captured(
        lambda body: httpx.Response(
            200,
            json={
                "data": {
                    "viewer": {
                        "lists": {
                            "nodes": [
                                {
                                    "id": "UL_1",
                                    "name": "Android",
                                    "slug": "android",
                                    "description": "",
                                    "isPrivate": False,
                                }
                            ]
                        }
                    }
                }
            },
        )
    )

    async with client:
        summaries = await client.fetch_star_list_summaries()

    assert [entry.name for entry in summaries] == ["Android"]
    # One request, and no itemPageSize variable — items were never selected.
    assert len(calls) == 1
    assert "itemPageSize" not in calls[0]
