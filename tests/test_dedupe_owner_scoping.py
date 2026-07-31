"""Dedupe lookups must be scoped to the request's owner.

The unique index is (user_id, dedupe_hash), and the x_bookmarks ingestor writes
rows carrying a dedupe_hash with a NULL user_id and status X_IMPORTED -- rows
that never have a summary. An unscoped lookup could therefore return the
bookmark row instead of the owner's completed request, report a cache miss and
re-summarize a URL that had already been paid for. A post-fetch ownership check
cannot repair that: it rejects the wrong row without ever finding the right one.

Also CLAUDE.md rule 12: the user_id predicate is a deliberate defence-in-depth
IDOR guard and must not be dropped.
"""

from __future__ import annotations

import inspect

import pytest

from app.application.ports.requests import RequestRepositoryPort
from app.infrastructure.persistence.repositories.request_repository import (
    RequestRepositoryAdapter,
)


@pytest.mark.parametrize(
    "method",
    ["async_get_request_by_dedupe_hash", "async_find_recent_request_by_dedupe"],
)
def test_lookup_requires_an_explicit_owner(method: str) -> None:
    """Keyword-only and no default, so it cannot be omitted by accident."""
    sig = inspect.signature(getattr(RequestRepositoryAdapter, method))
    assert "user_id" in sig.parameters, f"{method} lost its owner predicate"
    param = sig.parameters["user_id"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty, (
        "a default would let the guard be dropped silently, which is how it went missing"
    )


def test_port_matches_the_adapter() -> None:
    port_sig = inspect.signature(RequestRepositoryPort.async_get_request_by_dedupe_hash)
    assert "user_id" in port_sig.parameters


@pytest.mark.parametrize(
    "method",
    ["async_get_request_by_dedupe_hash", "async_find_recent_request_by_dedupe"],
)
def test_query_filters_and_orders_deterministically(method: str) -> None:
    """Both the predicate and a stable pick are needed.

    scalar() over an unordered multi-row match returns whichever row the planner
    hands back first, so the answer could differ between identical calls.
    """
    source = inspect.getsource(getattr(RequestRepositoryAdapter, method))
    assert "Request.user_id == user_id" in source
    assert "order_by" in source
    assert "limit(1)" in source


@pytest.mark.asyncio
async def test_owner_scoped_lookup_skips_a_null_user_bookmark_row() -> None:
    """The concrete failure: an x_imported row shadowing the owner's request."""
    captured: dict[str, object] = {}

    class _Session:
        async def scalar(self, stmt: object) -> None:
            captured["sql"] = str(stmt)

        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc: object) -> None:
            return None

    class _Db:
        def session(self) -> _Session:
            return _Session()

    repo = RequestRepositoryAdapter.__new__(RequestRepositoryAdapter)
    repo._database = _Db()  # type: ignore[attr-defined]

    await repo.async_get_request_by_dedupe_hash("abc", user_id=42)

    sql = str(captured["sql"])
    assert "user_id" in sql, "the owner predicate never reached the query"
    assert "ORDER BY" in sql.upper()
