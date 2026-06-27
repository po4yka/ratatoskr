"""Shared fixtures for AI account backup tests (no live network/DB)."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from app.adapters.content.browser_auth.authenticated_context import FetchResponse


def json_response(obj: object, status: int = 200) -> FetchResponse:
    """Build a FetchResponse whose body is the JSON encoding of ``obj``."""
    return FetchResponse(status=status, body_bytes=json.dumps(obj).encode("utf-8"))


def raw_response(body: bytes, status: int = 200) -> FetchResponse:
    return FetchResponse(status=status, body_bytes=body)


class FakeAuthedFetcher:
    """In-memory ``AuthedFetcher``.

    ``handler(url)`` returns a ``FetchResponse``, or an ``Exception`` instance to
    be raised, or ``None`` for a 404. Every call is recorded in ``.calls``.
    """

    def __init__(self, handler: Callable[[str], object]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> FetchResponse:
        self.calls.append((url, headers))
        result = self._handler(url)
        if isinstance(result, BaseException):
            raise result
        if result is None:
            return FetchResponse(status=404, body_bytes=b"{}")
        assert isinstance(result, FetchResponse)
        return result


@pytest.fixture
def fake_fetcher() -> Callable[[Callable[[str], object]], FakeAuthedFetcher]:
    def _make(handler: Callable[[str], object]) -> FakeAuthedFetcher:
        return FakeAuthedFetcher(handler)

    return _make
