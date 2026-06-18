from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from app.db.session import Database
from app.infrastructure.rules.context import RuleContextAdapter
from app.infrastructure.rules.http_webhook_dispatcher import HttpWebhookDispatchAdapter
from app.infrastructure.rules.in_memory_rate_limiter import InMemoryRuleRateLimiter


class _ScalarResult:
    def __iter__(self) -> object:
        return iter(["ai", "news"])


class _Session:
    async def get(self, model: object, item_id: int) -> object:
        name = getattr(model, "__name__", "")
        if name == "Summary":
            return SimpleNamespace(
                id=item_id,
                request_id=10,
                json_payload={"summary_250": "Summary", "metadata": {"title": "Title"}},
                lang="en",
                is_read=False,
                is_favorited=True,
                created_at="2026-01-01",
            )
        return SimpleNamespace(normalized_url="https://example.test", input_url="https://raw.test")

    async def scalars(self, stmt: object) -> _ScalarResult:
        return _ScalarResult()


class _Database:
    @asynccontextmanager
    async def session(self) -> AsyncIterator[_Session]:
        yield _Session()


@pytest.mark.asyncio
async def test_rule_context_adapter_merges_persisted_and_event_context() -> None:
    result = await RuleContextAdapter(cast("Database", _Database())).async_build_context(
        {
            "summary_id": 1,
            "url": "https://override.test",
            "tags": ["event"],
            "reading_time": 3,
        }
    )

    assert result.url == "https://override.test"
    assert result.tags == ["event"]
    assert result.reading_time == 3
    assert result.summary_snapshot is not None
    assert result.summary_snapshot["id"] == 1


@pytest.mark.asyncio
async def test_in_memory_rate_limiter_blocks_after_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1000.0
    monkeypatch.setattr("app.infrastructure.rules.in_memory_rate_limiter.time.time", lambda: now)
    limiter = InMemoryRuleRateLimiter()

    assert await limiter.async_allow_execution(1, limit=2, window_seconds=60)
    assert await limiter.async_allow_execution(1, limit=2, window_seconds=60)
    assert not await limiter.async_allow_execution(1, limit=2, window_seconds=60)

    now = 2000.0
    assert await limiter.async_allow_execution(1, limit=2, window_seconds=60)


@pytest.mark.asyncio
async def test_http_webhook_dispatcher_blocks_unsafe_url() -> None:
    with pytest.raises(ValueError, match="SSRF"):
        await HttpWebhookDispatchAdapter().async_dispatch("http://127.0.0.1/hook", {"x": 1})


@pytest.mark.asyncio
async def test_http_webhook_dispatcher_uses_safe_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class _Response:
        status_code = 204

        def raise_for_status(self) -> None:
            calls["raise_for_status"] = True

    class _Client:
        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *_exc: object) -> None:
            calls["closed"] = True

        async def post(self, url: str, *, json: dict) -> _Response:
            calls["url"] = url
            calls["json"] = json
            return _Response()

    def _safe_client_factory(**kwargs: object) -> _Client:
        calls["client_kwargs"] = kwargs
        return _Client()

    monkeypatch.setattr(
        "app.infrastructure.rules.http_webhook_dispatcher.is_webhook_url_safe",
        lambda _url: (True, None),
    )
    monkeypatch.setattr(
        "app.infrastructure.rules.http_webhook_dispatcher.make_safe_async_client",
        _safe_client_factory,
    )

    status_code = await HttpWebhookDispatchAdapter().async_dispatch(
        "https://example.com/hook", {"x": 1}
    )

    assert status_code == 204
    assert calls["url"] == "https://example.com/hook"
    assert calls["json"] == {"x": 1}
    assert calls["raise_for_status"] is True
    assert calls["closed"] is True
    assert calls["client_kwargs"] == {"timeout": 5.0, "follow_redirects": False}


@pytest.mark.asyncio
async def test_http_webhook_dispatcher_blocks_dns_rebinding_at_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.infrastructure.rules.http_webhook_dispatcher.is_webhook_url_safe",
        lambda _url: (True, None),
    )
    monkeypatch.setattr(
        "app.security.ssrf.socket.getaddrinfo",
        lambda host, port, **_kwargs: [
            (0, 0, 0, "", ("10.0.0.1", port)),
        ],
    )

    with pytest.raises(httpx.ConnectError, match="SSRF blocked"):
        await HttpWebhookDispatchAdapter().async_dispatch("https://rebind.example/hook", {"x": 1})
