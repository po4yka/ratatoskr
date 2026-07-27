"""Tests for GET /v1/search/repositories endpoint (US-029).

Requires TEST_DATABASE_URL (skipped otherwise).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from app.api.routers.auth.tokens import create_access_token

_USER_ID = 901_000_001


@pytest_asyncio.fixture(autouse=True)
async def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_USER_IDS", str(_USER_ID))
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "")


def _auth() -> dict[str, str]:
    token = create_access_token(_USER_ID, client_id="test")
    return {"Authorization": f"Bearer {token}"}


def _fake_search_result(count: int = 3) -> Any:
    """Return a mock RepositorySearchResults with `count` hits."""
    from app.infrastructure.search.repository_search_service import (
        RepositorySearchResult,
        RepositorySearchResults,
    )

    items = [
        RepositorySearchResult(
            repository_id=i + 1,
            github_id=100_000 + i,
            full_name=f"user/repo-{i}",
            owner="user",
            name=f"repo-{i}",
            description=f"Description {i}",
            primary_language="Python",
            topics=["ml"],
            stars=10 * (i + 1),
            is_starred=False,
            pushed_at=datetime(2024, 1, i + 1, tzinfo=timezone.utc),
            last_synced_at=datetime(2024, 2, i + 1, tzinfo=timezone.utc),
            distance=0.1 * (i + 1),
        )
        for i in range(count)
    ]
    return RepositorySearchResults(
        items=items,
        total=count,
        limit=20,
        offset=0,
    )


# ---------------------------------------------------------------------------
# 1. search returns results
# ---------------------------------------------------------------------------


async def test_search_returns_results(client: Any, db: Any) -> None:
    mock_service = MagicMock()
    mock_service.search = AsyncMock(return_value=_fake_search_result(3))

    from app.api.main import app
    from app.api.routers.content.search import _get_repo_search_service

    app.dependency_overrides[_get_repo_search_service] = lambda: mock_service
    try:
        resp = client.get("/v1/search/repositories?q=machine+learning", headers=_auth())
    finally:
        app.dependency_overrides.pop(_get_repo_search_service, None)

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["query"] == "machine learning"
    assert len(data["results"]) == 3
    assert data["pagination"]["total"] == 3


async def test_search_preserves_sync_timestamp_when_repository_was_never_pushed(
    client: Any,
    db: Any,
) -> None:
    search_result = _fake_search_result(1)
    search_result.items[0] = replace(search_result.items[0], pushed_at=None)
    mock_service = MagicMock()
    mock_service.search = AsyncMock(return_value=search_result)

    from app.api.main import app
    from app.api.routers.content.search import _get_repo_search_service

    app.dependency_overrides[_get_repo_search_service] = lambda: mock_service
    try:
        resp = client.get("/v1/search/repositories?q=python", headers=_auth())
    finally:
        app.dependency_overrides.pop(_get_repo_search_service, None)

    assert resp.status_code == 200
    result = resp.json()["data"]["results"][0]
    assert result["pushedAt"] is None
    assert result["lastSyncedAt"] == "2024-02-01T00:00:00Z"


# ---------------------------------------------------------------------------
# 2. user_id is propagated to the search service
# ---------------------------------------------------------------------------


async def test_search_user_id_propagated_to_service(client: Any, db: Any) -> None:
    captured: dict[str, Any] = {}

    async def _capture_search(query: str, *, user_id: int, **kwargs: Any) -> Any:
        captured["user_id"] = user_id
        return _fake_search_result(0)

    mock_service = MagicMock()
    mock_service.search = _capture_search

    from app.api.main import app
    from app.api.routers.content.search import _get_repo_search_service

    app.dependency_overrides[_get_repo_search_service] = lambda: mock_service
    try:
        resp = client.get("/v1/search/repositories?q=python", headers=_auth())
    finally:
        app.dependency_overrides.pop(_get_repo_search_service, None)

    assert resp.status_code == 200
    assert captured["user_id"] == _USER_ID


# ---------------------------------------------------------------------------
# 3. short query → 422
# ---------------------------------------------------------------------------


def test_search_validates_query_min_length(client: Any, db: Any) -> None:
    from app.api.main import app
    from app.api.routers.content.search import _get_repo_search_service

    app.dependency_overrides[_get_repo_search_service] = lambda: MagicMock()
    try:
        resp = client.get("/v1/search/repositories?q=x", headers=_auth())
    finally:
        app.dependency_overrides.pop(_get_repo_search_service, None)
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# 4. pagination parameters forwarded
# ---------------------------------------------------------------------------


async def test_search_pagination(client: Any, db: Any) -> None:
    captured: dict[str, Any] = {}

    async def _capture(query: str, **kwargs: Any) -> Any:
        captured["query"] = query
        captured.update(kwargs)
        from app.infrastructure.search.repository_search_service import RepositorySearchResults

        return RepositorySearchResults(items=[], total=50, limit=10, offset=10)

    mock_service = MagicMock()
    mock_service.search = _capture

    from app.api.main import app
    from app.api.routers.content.search import _get_repo_search_service

    app.dependency_overrides[_get_repo_search_service] = lambda: mock_service
    try:
        resp = client.get(
            "/v1/search/repositories?q=python&limit=10&offset=10",
            headers=_auth(),
        )
    finally:
        app.dependency_overrides.pop(_get_repo_search_service, None)

    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["pagination"]["total"] == 50
    assert data["pagination"]["hasMore"] is True  # offset(10) + len(0) < 50
