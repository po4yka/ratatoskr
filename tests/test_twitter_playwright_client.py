"""Tests for Playwright Twitter/X client helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.adapters.twitter.playwright_client import (
    _load_cookies_netscape,
    _merge_captured_tweets,
    _parse_tco_html_redirect,
    _response_matches_requested_tweet,
    resolve_tco_url,
)


def _make_tweet_result(
    tweet_id: str,
    text: str,
    author: str = "Test User",
    handle: str = "testuser",
) -> dict:
    return {
        "rest_id": tweet_id,
        "core": {
            "user_results": {
                "result": {
                    "legacy": {
                        "name": author,
                        "screen_name": handle,
                    }
                }
            }
        },
        "legacy": {
            "id_str": tweet_id,
            "full_text": text,
            "extended_entities": {"media": []},
        },
    }


def _make_thread_response(*tweet_results: dict) -> dict:
    entries = []
    for tr in tweet_results:
        entries.append(
            {
                "content": {
                    "itemContent": {
                        "tweet_results": {"result": tr},
                    }
                }
            }
        )
    return {
        "data": {
            "threaded_conversation_with_injections_v2": {"instructions": [{"entries": entries}]}
        }
    }


def test_merge_captured_tweets_preserves_global_order_across_responses() -> None:
    r1 = _make_thread_response(
        _make_tweet_result("1", "part 1"),
        _make_tweet_result("2", "part 2"),
    )
    r2 = _make_thread_response(
        _make_tweet_result("3", "part 3"),
        _make_tweet_result("4", "part 4"),
    )

    merged = _merge_captured_tweets([r1, r2])
    assert [t.tweet_id for t in merged] == ["1", "2", "3", "4"]
    assert [t.order for t in merged] == [0, 1, 2, 3]


def test_merge_captured_tweets_deduplicates_by_tweet_id() -> None:
    r1 = _make_thread_response(
        _make_tweet_result("1", "part 1"),
        _make_tweet_result("2", "part 2"),
    )
    r2 = _make_thread_response(
        _make_tweet_result("2", "part 2 duplicate"),
        _make_tweet_result("3", "part 3"),
    )

    merged = _merge_captured_tweets([r1, r2])
    assert [t.tweet_id for t in merged] == ["1", "2", "3"]
    assert [t.order for t in merged] == [0, 1, 2]


def test_response_matches_requested_tweet_with_encoded_variables() -> None:
    response_url = (
        "https://x.com/i/api/graphql/abc/TweetDetail?"
        "variables=%7B%22focalTweetId%22%3A%2212345%22%7D"
    )
    assert _response_matches_requested_tweet(response_url, "12345") is True
    assert _response_matches_requested_tweet(response_url, "99999") is False


def test_response_matches_requested_tweet_without_expectation() -> None:
    assert (
        _response_matches_requested_tweet("https://x.com/i/api/graphql/x/TweetDetail", None) is True
    )


def test_response_matches_requested_tweet_with_raw_query_value() -> None:
    response_url = "https://x.com/i/api/graphql/abc/TweetDetail?focalTweetId=67890"
    assert _response_matches_requested_tweet(response_url, "67890") is True
    assert _response_matches_requested_tweet(response_url, "11111") is False


def test_load_cookies_netscape_keeps_httponly_entries(tmp_path) -> None:
    cookies_file = tmp_path / "cookies.txt"
    cookies_file.write_text(
        "\n".join(
            [
                "# Netscape HTTP Cookie File",
                "#HttpOnly_.x.com\tTRUE\t/\tTRUE\t2147483647\tauth_token\tsecret",
                ".x.com\tTRUE\t/\tTRUE\t2147483647\tct0\tcsrf",
            ]
        )
    )

    cookies = _load_cookies_netscape(cookies_file)

    assert len(cookies) == 2
    assert cookies[0]["name"] == "auth_token"
    assert cookies[0]["httpOnly"] is True
    assert cookies[1]["name"] == "ct0"
    assert cookies[1]["httpOnly"] is False


def _tco_response(
    status: int = 200, *, location: str | None = None, text: str = ""
) -> SimpleNamespace:
    headers = {"location": location} if location else {}
    return SimpleNamespace(status_code=status, headers=headers, text=text)


def _patch_safe_client(monkeypatch, client_cls: type) -> None:
    monkeypatch.setattr(
        "app.adapters.twitter.playwright_client.make_safe_async_client",
        lambda *args, **kwargs: client_cls(),
    )


@pytest.mark.asyncio
async def test_resolve_tco_url_follows_redirect_with_per_hop_ssrf_check(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.adapters.twitter.playwright_client.is_url_safe_async",
        AsyncMock(return_value=(True, None)),
    )

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def head(self, url: str) -> SimpleNamespace:
            if "t.co" in url.lower():
                return _tco_response(301, location="https://resolved.example/final")
            return _tco_response(200)

    _patch_safe_client(monkeypatch, _FakeAsyncClient)

    assert await resolve_tco_url("http://t.co/abc123") == "https://resolved.example/final"
    assert await resolve_tco_url("HTTPS://t.co/abc123") == "https://resolved.example/final"
    assert await resolve_tco_url("https://example.com/nope") is None


@pytest.mark.asyncio
async def test_resolve_tco_url_blocks_redirect_to_internal_address(monkeypatch) -> None:
    """A t.co link redirecting to an internal address is not followed."""

    async def _fake_safe(url: str, **_kwargs) -> tuple[bool, str | None]:
        blocked = any(marker in url for marker in ("169.254.", "127.0.0.1", "localhost"))
        return (not blocked, "blocked private address" if blocked else None)

    monkeypatch.setattr("app.adapters.twitter.playwright_client.is_url_safe_async", _fake_safe)

    head_targets: list[str] = []

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def head(self, url: str) -> SimpleNamespace:
            head_targets.append(url)
            if "t.co" in url.lower():
                return _tco_response(301, location="http://169.254.169.254/latest/meta-data/")
            return _tco_response(200)

    _patch_safe_client(monkeypatch, _FakeAsyncClient)

    assert await resolve_tco_url("https://t.co/evil") is None
    # The internal address must never have been requested.
    assert not any("169.254." in target for target in head_targets)


class TestParseTcoHtmlRedirect:
    """Unit tests for t.co HTML fallback parsing."""

    def test_meta_refresh(self) -> None:
        html = '<html><head><meta http-equiv="refresh" content="0;URL=https://example.com/article"></head></html>'
        assert _parse_tco_html_redirect(html) == "https://example.com/article"

    def test_location_replace(self) -> None:
        html = '<script>location.replace("https://example.com/page")</script>'
        assert _parse_tco_html_redirect(html) == "https://example.com/page"

    def test_location_replace_escaped_slashes(self) -> None:
        html = "<script>location.replace('https:\\/\\/example.com\\/page')</script>"
        assert _parse_tco_html_redirect(html) == "https://example.com/page"

    def test_title_url(self) -> None:
        html = "<html><head><title>https://example.com/dest</title></head></html>"
        assert _parse_tco_html_redirect(html) == "https://example.com/dest"

    def test_title_non_url_ignored(self) -> None:
        html = "<html><head><title>Page Title</title></head></html>"
        assert _parse_tco_html_redirect(html) is None

    def test_empty_html(self) -> None:
        assert _parse_tco_html_redirect("") is None

    def test_priority_meta_over_location(self) -> None:
        html = (
            '<meta http-equiv="refresh" content="0;URL=https://first.com">'
            '<script>location.replace("https://second.com")</script>'
        )
        assert _parse_tco_html_redirect(html) == "https://first.com"


@pytest.mark.asyncio
async def test_resolve_tco_url_html_fallback(monkeypatch) -> None:
    """When HEAD stays on t.co, GET + HTML parsing should resolve."""
    monkeypatch.setattr(
        "app.adapters.twitter.playwright_client.is_url_safe_async",
        AsyncMock(return_value=(True, None)),
    )

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def head(self, url: str) -> SimpleNamespace:
            return _tco_response(200)  # stays on t.co, no redirect

        async def get(self, url: str) -> SimpleNamespace:
            return _tco_response(
                200,
                text='<script>location.replace("https://example.com/real")</script>',
            )

    _patch_safe_client(monkeypatch, _FakeAsyncClient)

    result = await resolve_tco_url("https://t.co/abc123")
    assert result == "https://example.com/real"


@pytest.mark.asyncio
async def test_resolve_tco_url_blocks_unsafe_html_destination(monkeypatch) -> None:
    """A destination parsed from the t.co HTML fallback is SSRF-checked."""

    async def _fake_safe(url: str, **_kwargs) -> tuple[bool, str | None]:
        blocked = "169.254." in url
        return (not blocked, "blocked private address" if blocked else None)

    monkeypatch.setattr("app.adapters.twitter.playwright_client.is_url_safe_async", _fake_safe)

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def head(self, url: str) -> SimpleNamespace:
            return _tco_response(200)  # stays on t.co

        async def get(self, url: str) -> SimpleNamespace:
            return _tco_response(
                200,
                text='<script>location.replace("http://169.254.169.254/")</script>',
            )

    _patch_safe_client(monkeypatch, _FakeAsyncClient)

    assert await resolve_tco_url("https://t.co/abc123") is None
