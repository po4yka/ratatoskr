from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.telegram.callback_action_store import CallbackActionStore


class _AsyncioStub:
    async def to_thread(self, fn: Any, *args: Any) -> Any:
        return fn(*args)


class _TimeStub:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)

    def time(self) -> float:
        return next(self._values)


def _make_store(**kwargs: Any) -> CallbackActionStore:
    return CallbackActionStore(
        request_repo=kwargs.pop("request_repo", MagicMock()),
        summary_repo=kwargs.pop("summary_repo", MagicMock()),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_load_summary_payload_async_supports_request_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(
        asyncio_module=_AsyncioStub(),
        time_module=_TimeStub(100.0),
    )

    summary_dict = {
        "id": 5,
        "request": 7,
        "lang": "en",
        "json_payload": {"title": "Example"},
        "insights_json": {"topic_overview": "Overview"},
    }
    request_dict = {"normalized_url": "https://example.com/item"}

    monkeypatch.setattr(
        store._summary_repo, "async_get_summary_by_request", AsyncMock(return_value=summary_dict)
    )
    monkeypatch.setattr(
        store._request_repo, "async_get_request_by_id", AsyncMock(return_value=request_dict)
    )

    payload = await store.load_summary_payload("req:7")

    assert payload == {
        "id": "5",
        "request_id": 7,
        "url": "https://example.com/item",
        "lang": "en",
        "insights": {"topic_overview": "Overview"},
        "title": "Example",
    }


@pytest.mark.asyncio
async def test_load_summary_payload_async_sanitizes_non_dict_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(
        asyncio_module=_AsyncioStub(),
        time_module=_TimeStub(100.0),
    )

    summary_dict = {
        "id": 5,
        "request": 7,
        "lang": "en",
        "json_payload": "not-a-dict",
        "insights_json": "also-not-a-dict",
    }

    context = {"summary": summary_dict, "request": None}
    monkeypatch.setattr(
        store._summary_repo, "async_get_summary_context_by_id", AsyncMock(return_value=context)
    )

    payload = await store.load_summary_payload("5")

    assert payload == {
        "id": "5",
        "request_id": 7,
        "url": None,
        "lang": "en",
        "insights": None,
    }


@pytest.mark.asyncio
async def test_load_summary_payload_uses_cache_within_ttl() -> None:
    store = _make_store(
        asyncio_module=_AsyncioStub(),
        time_module=_TimeStub(100.0, 100.5),
        summary_cache_ttl=30.0,
        summary_cache_max=5,
    )
    calls: list[str] = []

    def _loader(summary_id: str) -> dict[str, Any] | None:
        calls.append(summary_id)
        return {"id": summary_id}

    first = await store.load_summary_payload("42", loader=_loader)
    second = await store.load_summary_payload("42", loader=_loader)

    assert first == {"id": "42"}
    assert second == {"id": "42"}
    assert calls == ["42"]


@pytest.mark.asyncio
async def test_load_summary_payload_evicts_oldest_entry_when_cache_is_full() -> None:
    store = _make_store(
        asyncio_module=_AsyncioStub(),
        time_module=_TimeStub(1.0, 2.0, 3.0),
        summary_cache_ttl=30.0,
        summary_cache_max=2,
    )

    def _loader(summary_id: str) -> dict[str, Any] | None:
        return {"id": summary_id}

    await store.load_summary_payload("one", loader=_loader)
    await store.load_summary_payload("two", loader=_loader)
    await store.load_summary_payload("three", loader=_loader)

    assert list(store._summary_cache) == ["two", "three"]


@pytest.mark.asyncio
async def test_toggle_save_invalidates_exact_cache_key(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store(asyncio_module=_AsyncioStub())
    store._summary_cache["42"] = (100.0, {"id": "42"})
    monkeypatch.setattr(
        store._summary_repo, "async_toggle_favorite_for_user", AsyncMock(return_value=True)
    )

    result = await store.toggle_save("42", user_id=100)

    assert result is True
    assert "42" not in store._summary_cache


@pytest.mark.asyncio
async def test_lookup_retry_url_returns_url_for_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store()
    monkeypatch.setattr(
        store._request_repo,
        "async_get_latest_request_by_correlation_id",
        AsyncMock(return_value={"user_id": 100, "input_url": "https://example.com/x"}),
    )

    assert await store.lookup_retry_url("orig-cid", 100) == "https://example.com/x"


@pytest.mark.asyncio
async def test_lookup_retry_url_denies_other_user(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store()
    monkeypatch.setattr(
        store._request_repo,
        "async_get_latest_request_by_correlation_id",
        AsyncMock(return_value={"user_id": 100, "input_url": "https://example.com/x"}),
    )

    # A different user replaying the embedded correlation id gets nothing.
    assert await store.lookup_retry_url("orig-cid", 999) is None


@pytest.mark.asyncio
async def test_summary_belongs_to_user_checks_request_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store()
    monkeypatch.setattr(
        store._summary_repo,
        "async_get_summary_context_by_id",
        AsyncMock(return_value={"request": {"user_id": 100}}),
    )

    assert await store.summary_belongs_to_user("42", 100) is True
    assert await store.summary_belongs_to_user("42", 999) is False


@pytest.mark.asyncio
async def test_summary_belongs_to_user_req_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store()
    monkeypatch.setattr(
        store._request_repo,
        "async_get_request_by_id",
        AsyncMock(return_value={"user_id": 100}),
    )

    assert await store.summary_belongs_to_user("req:7", 100) is True
    assert await store.summary_belongs_to_user("req:7", 1) is False


@pytest.mark.asyncio
async def test_summary_belongs_to_user_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _make_store()

    # Unparseable id never reaches the repo.
    assert await store.summary_belongs_to_user("not-an-int", 100) is False

    # Missing summary / no owning request denies.
    monkeypatch.setattr(
        store._summary_repo,
        "async_get_summary_context_by_id",
        AsyncMock(return_value=None),
    )
    assert await store.summary_belongs_to_user("42", 100) is False
