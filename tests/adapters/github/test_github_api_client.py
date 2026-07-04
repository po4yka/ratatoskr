"""Tests for GitHubAPIClient — respx-mocked HTTP, retry logic, pagination."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import httpx
import pytest
import respx

from app.adapters.github.exceptions import (
    GitHubAuthError,
    GitHubRateLimitError,
    GitHubServerError,
)
from app.adapters.github.github_api_client import GitHubAPIClient

FIXTURES = Path(__file__).parent / "fixtures"

REPO_URL = "https://api.github.com/repos/tiangolo/fastapi"
README_URL = "https://api.github.com/repos/tiangolo/fastapi/readme"
LATEST_RELEASE_URL = "https://api.github.com/repos/tiangolo/fastapi/releases/latest"
STARRED_URL = "https://api.github.com/user/starred"
GISTS_URL = "https://api.github.com/gists"
USER_URL = "https://api.github.com/user"


def _repo_json() -> dict:
    return json.loads((FIXTURES / "repo_fastapi.json").read_text())


def _starred_page1() -> list:
    return json.loads((FIXTURES / "starred_page1.json").read_text())


def _starred_page2() -> list:
    return json.loads((FIXTURES / "starred_page2.json").read_text())


def _gists_page1() -> list:
    return json.loads((FIXTURES / "gists_page1.json").read_text())


def _gists_page2() -> list:
    return json.loads((FIXTURES / "gists_page2.json").read_text())


def _make_client(**kwargs) -> GitHubAPIClient:
    return GitHubAPIClient(
        "ghp_test_token",
        backoff_min_sec=0.0,
        backoff_max_sec=0.0,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 1. Happy path: get_repo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_repo_happy_path() -> None:
    async with respx.mock:
        respx.get(REPO_URL).mock(return_value=httpx.Response(200, json=_repo_json()))

        async with _make_client() as gh:
            repo = await gh.get_repo("tiangolo", "fastapi")

    assert repo.name == "fastapi"
    assert repo.full_name == "tiangolo/fastapi"
    assert repo.owner.login == "tiangolo"
    assert repo.owner.type == "User"
    assert repo.stargazers_count == 75000
    assert repo.language == "Python"
    assert repo.license is not None
    assert repo.license.spdx_id == "MIT"
    assert "fastapi" in repo.topics


# ---------------------------------------------------------------------------
# 2. get_readme conditional fetch: 200 captures ETag, 304 preserves it, 404 empty
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_readme_returns_new_etag_on_200() -> None:
    markdown = "# FastAPI\n\nA modern web framework."

    async with respx.mock:
        respx.get(README_URL).mock(
            return_value=httpx.Response(200, text=markdown, headers={"ETag": '"abc123"'})
        )

        async with _make_client() as gh:
            result = await gh.get_readme("tiangolo", "fastapi")

    assert result.content == markdown
    assert result.etag == '"abc123"'
    assert result.not_modified is False


@pytest.mark.asyncio
async def test_get_readme_304_sends_if_none_match_and_reports_not_modified() -> None:
    captured: dict[str, str] = {}

    def _responder(request: httpx.Request) -> httpx.Response:
        captured["if_none_match"] = request.headers.get("If-None-Match", "")
        return httpx.Response(304)

    async with respx.mock:
        respx.get(README_URL).mock(side_effect=_responder)

        async with _make_client() as gh:
            result = await gh.get_readme("tiangolo", "fastapi", etag='"abc123"')

    assert captured["if_none_match"] == '"abc123"'
    assert result.not_modified is True
    assert result.content is None
    assert result.etag == '"abc123"'


@pytest.mark.asyncio
async def test_get_readme_404_returns_empty_result() -> None:
    async with respx.mock:
        respx.get(README_URL).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))

        async with _make_client() as gh:
            result = await gh.get_readme("tiangolo", "fastapi")

    assert result.content is None
    assert result.etag is None
    assert result.not_modified is False


@pytest.mark.asyncio
async def test_get_latest_release_returns_release_or_none() -> None:
    async with respx.mock:
        respx.get(LATEST_RELEASE_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": 123,
                    "tag_name": "v1.2.3",
                    "name": "Release 1.2.3",
                    "html_url": "https://github.com/tiangolo/fastapi/releases/tag/v1.2.3",
                    "published_at": "2024-01-02T03:04:05Z",
                },
            )
        )

        async with _make_client() as gh:
            result = await gh.get_latest_release("tiangolo", "fastapi")

    assert result is not None
    assert result.tag_name == "v1.2.3"


@pytest.mark.asyncio
async def test_get_latest_release_404_returns_none() -> None:
    async with respx.mock:
        respx.get(LATEST_RELEASE_URL).mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )

        async with _make_client() as gh:
            result = await gh.get_latest_release("tiangolo", "fastapi")

    assert result is None


# ---------------------------------------------------------------------------
# 4. 401 raises GitHubAuthError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_401_raises_github_auth_error() -> None:
    async with respx.mock:
        respx.get(REPO_URL).mock(
            return_value=httpx.Response(401, json={"message": "Bad credentials"})
        )

        async with _make_client() as gh:
            with pytest.raises(GitHubAuthError):
                await gh.get_repo("tiangolo", "fastapi")


# ---------------------------------------------------------------------------
# 5. 403 rate limit raises GitHubRateLimitError with correct reset_epoch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_403_rate_limit_raises_github_rate_limit_error_with_reset() -> None:
    reset_ts = 1735689600

    async with respx.mock:
        respx.get(REPO_URL).mock(
            return_value=httpx.Response(
                403,
                json={"message": "API rate limit exceeded"},
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_ts),
                },
            )
        )

        async with _make_client() as gh:
            with pytest.raises(GitHubRateLimitError) as exc_info:
                await gh.get_repo("tiangolo", "fastapi")

    assert exc_info.value.reset_epoch == reset_ts


# ---------------------------------------------------------------------------
# 5b. Secondary rate limit (403 + Retry-After, remaining != 0) and 429
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_403_secondary_limit_retry_after_seconds() -> None:
    async with respx.mock:
        respx.get(REPO_URL).mock(
            return_value=httpx.Response(
                403,
                json={"message": "You have exceeded a secondary rate limit"},
                headers={"X-RateLimit-Remaining": "57", "Retry-After": "30"},
            )
        )

        async with _make_client() as gh:
            with pytest.raises(GitHubRateLimitError) as exc_info:
                await gh.get_repo("tiangolo", "fastapi")

    assert 25 <= exc_info.value.reset_epoch - int(time.time()) <= 31


@pytest.mark.asyncio
async def test_403_secondary_limit_retry_after_http_date() -> None:
    http_date = "Wed, 21 Oct 2099 07:28:00 GMT"
    expected = int(parsedate_to_datetime(http_date).timestamp())

    async with respx.mock:
        respx.get(REPO_URL).mock(
            return_value=httpx.Response(
                403,
                json={"message": "secondary rate limit"},
                headers={"X-RateLimit-Remaining": "57", "Retry-After": http_date},
            )
        )

        async with _make_client() as gh:
            with pytest.raises(GitHubRateLimitError) as exc_info:
                await gh.get_repo("tiangolo", "fastapi")

    assert exc_info.value.reset_epoch == expected


@pytest.mark.asyncio
async def test_429_raises_github_rate_limit_error() -> None:
    async with respx.mock:
        respx.get(REPO_URL).mock(
            return_value=httpx.Response(
                429,
                json={"message": "Too Many Requests"},
                headers={"Retry-After": "45"},
            )
        )

        async with _make_client() as gh:
            with pytest.raises(GitHubRateLimitError) as exc_info:
                await gh.get_repo("tiangolo", "fastapi")

    assert 40 <= exc_info.value.reset_epoch - int(time.time()) <= 46


# ---------------------------------------------------------------------------
# 6. 5xx retries then succeeds on 3rd attempt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_retries_then_succeeds() -> None:
    async with respx.mock:
        route = respx.get(REPO_URL).mock(
            side_effect=[
                httpx.Response(503, json={"message": "Service Unavailable"}),
                httpx.Response(503, json={"message": "Service Unavailable"}),
                httpx.Response(200, json=_repo_json()),
            ]
        )

        async with _make_client(max_retries=3) as gh:
            repo = await gh.get_repo("tiangolo", "fastapi")

    assert repo.name == "fastapi"
    assert route.call_count == 3


# ---------------------------------------------------------------------------
# 7. 5xx retries exhausted raises GitHubServerError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5xx_retries_exhausted_raises_github_server_error() -> None:
    async with respx.mock:
        respx.get(REPO_URL).mock(
            return_value=httpx.Response(503, json={"message": "Service Unavailable"})
        )

        async with _make_client(max_retries=3) as gh:
            with pytest.raises(GitHubServerError):
                await gh.get_repo("tiangolo", "fastapi")


# ---------------------------------------------------------------------------
# 8. list_starred paginates via Link header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_starred_paginates_via_link_header() -> None:
    # The client appends ?sort=created&direction=desc&per_page=100 on page 1
    starred_page1_url = f"{STARRED_URL}?sort=created&direction=desc&per_page=100"
    page2_url = "https://api.github.com/user/starred?page=2&per_page=100"

    router = respx.MockRouter(assert_all_called=False)
    router.get(starred_page1_url).mock(
        return_value=httpx.Response(
            200,
            json=_starred_page1(),
            headers={"Link": f'<{page2_url}>; rel="next"'},
        )
    )
    router.get(page2_url).mock(return_value=httpx.Response(200, json=_starred_page2()))

    async with router:
        async with _make_client() as gh:
            iterator = await gh.list_starred()
            items = [item async for item in iterator]

    assert len(items) == 3
    assert items[0].repo.name == "repo-a"
    assert items[1].repo.name == "repo-b"
    assert items[2].repo.name == "repo-c"


# ---------------------------------------------------------------------------
# 8b. list_starred refuses a next-link pointing at a non-GitHub host
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_starred_refuses_next_link_to_non_github_host() -> None:
    starred_page1_url = f"{STARRED_URL}?sort=created&direction=desc&per_page=100"
    evil_url = "https://evil.example.com/user/starred?page=2&per_page=100"

    router = respx.MockRouter(assert_all_called=False)
    router.get(starred_page1_url).mock(
        return_value=httpx.Response(
            200,
            json=_starred_page1(),
            headers={"Link": f'<{evil_url}>; rel="next"'},
        )
    )
    evil_route = router.get(evil_url).mock(return_value=httpx.Response(200, json=[]))

    async with router:
        async with _make_client() as gh:
            iterator = await gh.list_starred()
            with pytest.raises(GitHubServerError, match="non-GitHub host"):
                _ = [item async for item in iterator]

    # The attacker-controlled host must never be dereferenced — the bearer
    # token must never leave for a non-GitHub host.
    assert evil_route.call_count == 0


# ---------------------------------------------------------------------------
# 9. list_starred since early-exits when cutoff hit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_starred_since_early_exits() -> None:
    # Items are in desc order: repo-a (Jan 20), repo-b (Jan 15)
    # Since Jan 18 → only repo-a should be yielded
    since = datetime(2024, 1, 18, 0, 0, 0, tzinfo=timezone.utc)
    starred_page1_url = f"{STARRED_URL}?sort=created&direction=desc&per_page=100"

    router = respx.MockRouter(assert_all_called=False)
    router.get(starred_page1_url).mock(return_value=httpx.Response(200, json=_starred_page1()))

    async with router:
        async with _make_client() as gh:
            iterator = await gh.list_starred(since=since)
            items = [item async for item in iterator]

    assert len(items) == 1
    assert items[0].repo.name == "repo-a"


# ---------------------------------------------------------------------------
# 10. Authorization header is never logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorization_header_redacted_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    token = "ghp_super_secret_token_12345"

    async with respx.mock:
        respx.get(REPO_URL).mock(return_value=httpx.Response(200, json=_repo_json()))

        with caplog.at_level(logging.DEBUG):
            async with GitHubAPIClient(token, backoff_min_sec=0.0, backoff_max_sec=0.0) as gh:
                await gh.get_repo("tiangolo", "fastapi")

    # The raw token must not appear in any log record
    all_log_text = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert token not in all_log_text, "Token found in log output — redaction failed"
    request_record = next(r for r in caplog.records if r.message == "github_api_request")
    request_headers = request_record.__dict__["request_headers"]
    assert request_headers["authorization"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# 11. list_gists returns all gists on a single page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_gists_single_page() -> None:
    gists_url = f"{GISTS_URL}?per_page=100"

    async with respx.mock:
        respx.get(gists_url).mock(return_value=httpx.Response(200, json=_gists_page1()))

        async with _make_client() as gh:
            gists = await gh.list_gists()

    assert len(gists) == 2
    assert gists[0].id == "abc123def456aaa"
    assert gists[0].git_pull_url == "https://gist.github.com/abc123def456aaa.git"
    assert gists[0].description == "My useful snippet"
    assert gists[1].id == "bbb222ccc333ddd"
    assert gists[1].description == ""


# ---------------------------------------------------------------------------
# 12. list_gists paginates via Link header
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_gists_paginates_via_link_header() -> None:
    gists_page1_url = f"{GISTS_URL}?per_page=100"
    page2_url = "https://api.github.com/gists?page=2&per_page=100"

    router = respx.MockRouter(assert_all_called=False)
    router.get(gists_page1_url).mock(
        return_value=httpx.Response(
            200,
            json=_gists_page1(),
            headers={"Link": f'<{page2_url}>; rel="next"'},
        )
    )
    router.get(page2_url).mock(return_value=httpx.Response(200, json=_gists_page2()))

    async with router:
        async with _make_client() as gh:
            gists = await gh.list_gists()

    assert len(gists) == 3
    assert gists[0].id == "abc123def456aaa"
    assert gists[1].id == "bbb222ccc333ddd"
    assert gists[2].id == "eee444fff555ggg"


# ---------------------------------------------------------------------------
# 13. list_gists returns empty list when no gists exist
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_gists_empty() -> None:
    gists_url = f"{GISTS_URL}?per_page=100"

    async with respx.mock:
        respx.get(gists_url).mock(return_value=httpx.Response(200, json=[]))

        async with _make_client() as gh:
            gists = await gh.list_gists()

    assert gists == []
