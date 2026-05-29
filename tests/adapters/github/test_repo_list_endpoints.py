"""Tests for GitHubAPIClient.list_owned_repos and list_watched_repos — pagination and happy path."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.adapters.github.github_api_client import GitHubAPIClient

FIXTURES = Path(__file__).parent / "fixtures"

OWNED_URL = "https://api.github.com/user/repos"
WATCHED_URL = "https://api.github.com/user/subscriptions"


def _owned_page1() -> list:
    return json.loads((FIXTURES / "owned_repos_page1.json").read_text())


def _owned_page2() -> list:
    return json.loads((FIXTURES / "owned_repos_page2.json").read_text())


def _watched_page1() -> list:
    return json.loads((FIXTURES / "watched_repos_page1.json").read_text())


def _make_client(**kwargs) -> GitHubAPIClient:
    return GitHubAPIClient(
        "ghp_test_token",
        backoff_min_sec=0.0,
        backoff_max_sec=0.0,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# list_owned_repos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_owned_repos_single_page() -> None:
    owned_url = f"{OWNED_URL}?affiliation=owner&per_page=100"

    async with respx.mock:
        respx.get(owned_url).mock(return_value=httpx.Response(200, json=_owned_page1()))

        async with _make_client() as gh:
            repos = await gh.list_owned_repos()

    assert len(repos) == 2
    assert repos[0].full_name == "alice/my-lib"
    assert repos[0].size == 1024
    assert repos[1].full_name == "alice/private-tool"
    assert repos[1].size == 512


@pytest.mark.asyncio
async def test_list_owned_repos_paginates_via_link_header() -> None:
    owned_page1_url = f"{OWNED_URL}?affiliation=owner&per_page=100"
    page2_url = "https://api.github.com/user/repos?page=2&per_page=100"

    router = respx.MockRouter(assert_all_called=False)
    router.get(owned_page1_url).mock(
        return_value=httpx.Response(
            200,
            json=_owned_page1(),
            headers={"Link": f'<{page2_url}>; rel="next"'},
        )
    )
    router.get(page2_url).mock(return_value=httpx.Response(200, json=_owned_page2()))

    async with router:
        async with _make_client() as gh:
            repos = await gh.list_owned_repos()

    assert len(repos) == 3
    assert repos[0].full_name == "alice/my-lib"
    assert repos[1].full_name == "alice/private-tool"
    assert repos[2].full_name == "alice/yet-another-repo"
    assert repos[2].size == 256


@pytest.mark.asyncio
async def test_list_owned_repos_empty() -> None:
    owned_url = f"{OWNED_URL}?affiliation=owner&per_page=100"

    async with respx.mock:
        respx.get(owned_url).mock(return_value=httpx.Response(200, json=[]))

        async with _make_client() as gh:
            repos = await gh.list_owned_repos()

    assert repos == []


# ---------------------------------------------------------------------------
# list_watched_repos
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_watched_repos_single_page() -> None:
    watched_url = f"{WATCHED_URL}?per_page=100"

    async with respx.mock:
        respx.get(watched_url).mock(return_value=httpx.Response(200, json=_watched_page1()))

        async with _make_client() as gh:
            repos = await gh.list_watched_repos()

    assert len(repos) == 2
    assert repos[0].full_name == "org/upstream-project"
    assert repos[0].size == 8192
    assert repos[1].full_name == "org/other-lib"
    assert repos[1].size == 128


@pytest.mark.asyncio
async def test_list_watched_repos_paginates_via_link_header() -> None:
    watched_page1_url = f"{WATCHED_URL}?per_page=100"
    page2_url = "https://api.github.com/user/subscriptions?page=2&per_page=100"

    # page2 returns same content as page1 for simplicity (testing that pagination fires)
    router = respx.MockRouter(assert_all_called=False)
    router.get(watched_page1_url).mock(
        return_value=httpx.Response(
            200,
            json=_watched_page1(),
            headers={"Link": f'<{page2_url}>; rel="next"'},
        )
    )
    router.get(page2_url).mock(return_value=httpx.Response(200, json=_watched_page1()))

    async with router:
        async with _make_client() as gh:
            repos = await gh.list_watched_repos()

    # Both pages returned 2 items each
    assert len(repos) == 4
    assert repos[0].full_name == "org/upstream-project"
    assert repos[2].full_name == "org/upstream-project"


@pytest.mark.asyncio
async def test_list_watched_repos_empty() -> None:
    watched_url = f"{WATCHED_URL}?per_page=100"

    async with respx.mock:
        respx.get(watched_url).mock(return_value=httpx.Response(200, json=[]))

        async with _make_client() as gh:
            repos = await gh.list_watched_repos()

    assert repos == []


# ---------------------------------------------------------------------------
# size field preserved on RepositoryDTO
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repository_dto_size_field_populated() -> None:
    """RepositoryDTO.size is populated from the GitHub API size field (KB)."""
    owned_url = f"{OWNED_URL}?affiliation=owner&per_page=100"

    async with respx.mock:
        respx.get(owned_url).mock(return_value=httpx.Response(200, json=_owned_page1()))

        async with _make_client() as gh:
            repos = await gh.list_owned_repos()

    # size should be an integer KB value as returned by GitHub
    for repo in repos:
        assert isinstance(repo.size, int)
        assert repo.size > 0


@pytest.mark.asyncio
async def test_repository_dto_size_defaults_to_zero_when_absent() -> None:
    """RepositoryDTO.size defaults to 0 when the field is not present in the payload."""
    owned_url = f"{OWNED_URL}?affiliation=owner&per_page=100"
    payload = [
        {
            "id": 9999,
            "name": "no-size-repo",
            "full_name": "user/no-size-repo",
            "owner": {"login": "user", "id": 1, "type": "User"},
            "html_url": "https://github.com/user/no-size-repo",
            "stargazers_count": 0,
            "forks_count": 0,
            "watchers_count": 0,
            # deliberately omitting "size"
        }
    ]

    async with respx.mock:
        respx.get(owned_url).mock(return_value=httpx.Response(200, json=payload))

        async with _make_client() as gh:
            repos = await gh.list_owned_repos()

    assert repos[0].size == 0
