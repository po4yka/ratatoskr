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
