"""Tests for redirected/canonical X Article link resolution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.adapters.twitter.article_link_resolver import resolve_twitter_article_link


def _fake_response(
    *,
    url: str,
    status_code: int = 200,
    content_type: str = "text/html",
    text: str = "",
) -> SimpleNamespace:
    return SimpleNamespace(
        url=url,
        status_code=status_code,
        headers={"content-type": content_type},
        text=text,
    )


@pytest.mark.asyncio
async def test_resolver_returns_path_match_for_direct_article_url() -> None:
    result = await resolve_twitter_article_link("https://x.com/i/article/12345")

    assert result.is_article is True
    assert result.reason == "path_match"
    assert result.article_id == "12345"
    assert result.canonical_url == "https://x.com/i/article/12345"


@pytest.mark.asyncio
async def test_resolver_detects_article_via_head_redirect(monkeypatch) -> None:
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def head(self, url: str) -> SimpleNamespace:
            # Manual redirect-following resolver: t.co -> 302 to article URL,
            # second hop returns 200 so the loop breaks and resolved_url is
            # set to the article URL.
            if "t.co" in url:
                resp = _fake_response(url=url, status_code=302)
                resp.headers = {
                    **resp.headers,
                    "location": "https://x.com/i/article/777",
                }
                return resp
            return _fake_response(url=url)

    monkeypatch.setattr("app.security.ssrf.httpx.AsyncClient", _FakeAsyncClient)
    # The real is_url_safe rejects t.co / x.com in CI because their public
    # DNS records resolve to addresses inside ranges the SSRF allowlist treats
    # as private. Stub it for tests that exercise the post-preflight flow.
    async def _fake_is_url_safe_async(_url: str) -> tuple[bool, str | None]:
        return (True, None)

    monkeypatch.setattr(
        "app.adapters.twitter.article_link_resolver.is_url_safe_async",
        _fake_is_url_safe_async,
    )

    result = await resolve_twitter_article_link("https://t.co/abc")

    assert result.is_article is True
    assert result.reason == "redirect_match"
    assert result.article_id == "777"
    assert result.canonical_url == "https://x.com/i/article/777"


@pytest.mark.asyncio
async def test_resolver_falls_back_to_get_and_uses_canonical_hint(monkeypatch) -> None:
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def head(self, url: str) -> SimpleNamespace:
            return _fake_response(url="https://x.com/some/path", status_code=405)

        async def get(self, url: str) -> SimpleNamespace:
            return _fake_response(
                url="https://x.com/some/path",
                text='<html><head><link rel="canonical" href="https://x.com/i/article/999"></head></html>',
            )

    monkeypatch.setattr("app.security.ssrf.httpx.AsyncClient", _FakeAsyncClient)
    # The real is_url_safe rejects t.co / x.com in CI because their public
    # DNS records resolve to addresses inside ranges the SSRF allowlist treats
    # as private. Stub it for tests that exercise the post-preflight flow.
    async def _fake_is_url_safe_async(_url: str) -> tuple[bool, str | None]:
        return (True, None)

    monkeypatch.setattr(
        "app.adapters.twitter.article_link_resolver.is_url_safe_async",
        _fake_is_url_safe_async,
    )

    result = await resolve_twitter_article_link("https://t.co/needs-get")

    assert result.is_article is True
    assert result.reason == "canonical_match"
    assert result.article_id == "999"
    assert result.canonical_url == "https://x.com/i/article/999"


@pytest.mark.asyncio
async def test_resolver_returns_not_article_for_unknown_hosts() -> None:
    result = await resolve_twitter_article_link("https://example.com/not-x")

    assert result.is_article is False
    assert result.reason == "not_article"
    assert result.article_id is None


@pytest.mark.asyncio
async def test_resolver_returns_resolve_failed_on_http_error(monkeypatch) -> None:
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncClient:
            return self

        async def __aexit__(self, exc_type, exc, tb) -> bool:
            return False

        async def head(self, url: str) -> SimpleNamespace:
            raise RuntimeError("network down")

    monkeypatch.setattr("app.security.ssrf.httpx.AsyncClient", _FakeAsyncClient)
    # The real is_url_safe rejects t.co / x.com in CI because their public
    # DNS records resolve to addresses inside ranges the SSRF allowlist treats
    # as private. Stub it for tests that exercise the post-preflight flow.
    async def _fake_is_url_safe_async(_url: str) -> tuple[bool, str | None]:
        return (True, None)

    monkeypatch.setattr(
        "app.adapters.twitter.article_link_resolver.is_url_safe_async",
        _fake_is_url_safe_async,
    )

    result = await resolve_twitter_article_link("https://t.co/fail")

    assert result.is_article is False
    assert result.reason == "resolve_failed"
