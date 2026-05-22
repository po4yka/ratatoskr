"""Tests for /v1/repositories endpoints (US-028).

Requires TEST_DATABASE_URL (skipped otherwise).
"""

from __future__ import annotations

import random
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from app.api.routers.auth.tokens import create_access_token
from app.db.models.repository import Repository, RepoSource
from app.db.session import Database

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_A_ID = 900_000_001
_USER_B_ID = 900_000_002


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(autouse=True)
async def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_USER_IDS", f"{_USER_A_ID},{_USER_B_ID}")
    monkeypatch.setenv("ALLOWED_CLIENT_IDS", "")


def _auth(user_id: int) -> dict[str, str]:
    token = create_access_token(user_id, client_id="test")
    return {"Authorization": f"Bearer {token}"}


async def _create_user(db: Database, user_id: int) -> None:
    from app.db.models import User

    async with db.transaction() as session:
        from sqlalchemy import select

        existing = await session.scalar(select(User).where(User.telegram_user_id == user_id))
        if existing is None:
            session.add(User(telegram_user_id=user_id, username=f"user_{user_id}"))
    await db.engine.dispose()


async def _create_repo(
    db: Database,
    *,
    user_id: int,
    full_name: str | None = None,
    primary_language: str | None = "Python",
    topics: list[str] | None = None,
    stars: int = 10,
    source: RepoSource = RepoSource.MANUAL,
    is_starred: bool = False,
    analysis_json: dict | None = None,
) -> Repository:
    rid = random.randint(1_000_000, 9_000_000)
    name_part = full_name or f"user/repo-{rid}"
    owner, name = name_part.split("/", 1) if "/" in name_part else ("user", name_part)
    repo = Repository(
        github_id=rid,
        owner=owner,
        name=name,
        full_name=f"{owner}/{name}",
        url=f"https://github.com/{owner}/{name}",
        stars=stars,
        forks=0,
        watchers=0,
        is_archived=False,
        is_fork=False,
        is_template=False,
        is_starred=is_starred,
        source=source,
        user_id=user_id,
        primary_language=primary_language,
        topics_json=topics or [],
        languages_json={},
        analysis_json=analysis_json,
        pending_analysis=False,
    )
    async with db.transaction() as session:
        session.add(repo)
        await session.flush()
        await session.refresh(repo)
    await db.engine.dispose()
    return repo


# ---------------------------------------------------------------------------
# 1. list returns only calling user's repos
# ---------------------------------------------------------------------------


async def test_list_returns_user_repos_only(client: Any, db: Database) -> None:
    await _create_user(db, _USER_A_ID)
    await _create_user(db, _USER_B_ID)
    for _ in range(3):
        await _create_repo(db, user_id=_USER_A_ID)
    for _ in range(2):
        await _create_repo(db, user_id=_USER_B_ID)

    resp = client.get("/v1/repositories", headers=_auth(_USER_A_ID))
    assert resp.status_code == 200
    data = resp.json()
    assert data["pagination"]["total"] == 3
    assert len(data["repositories"]) == 3


# ---------------------------------------------------------------------------
# 2. filter by language
# ---------------------------------------------------------------------------


async def test_list_filter_by_language(client: Any, db: Database) -> None:
    await _create_user(db, _USER_A_ID)
    await _create_repo(db, user_id=_USER_A_ID, primary_language="Python")
    await _create_repo(db, user_id=_USER_A_ID, primary_language="Python")
    await _create_repo(db, user_id=_USER_A_ID, primary_language="Go")

    resp = client.get("/v1/repositories?language=Python", headers=_auth(_USER_A_ID))
    assert resp.status_code == 200
    repos = resp.json()["repositories"]
    assert len(repos) == 2
    assert all(r["primary_language"] == "Python" for r in repos)


# ---------------------------------------------------------------------------
# 3. filter by topic
# ---------------------------------------------------------------------------


async def test_list_filter_by_topic(client: Any, db: Database) -> None:
    await _create_user(db, _USER_A_ID)
    await _create_repo(db, user_id=_USER_A_ID, topics=["webdev", "react"])
    await _create_repo(db, user_id=_USER_A_ID, topics=["webdev"])
    await _create_repo(db, user_id=_USER_A_ID, topics=["ml"])

    resp = client.get("/v1/repositories?topic=webdev", headers=_auth(_USER_A_ID))
    assert resp.status_code == 200
    repos = resp.json()["repositories"]
    assert len(repos) == 2


# ---------------------------------------------------------------------------
# 4. pagination
# ---------------------------------------------------------------------------


async def test_list_pagination(client: Any, db: Database) -> None:
    await _create_user(db, _USER_A_ID)
    for _ in range(25):
        await _create_repo(db, user_id=_USER_A_ID)

    resp = client.get("/v1/repositories?limit=10&offset=0", headers=_auth(_USER_A_ID))
    assert resp.status_code == 200
    body = resp.json()
    assert body["pagination"]["total"] == 25
    assert len(body["repositories"]) == 10
    assert body["pagination"]["hasMore"] is True

    resp2 = client.get("/v1/repositories?limit=10&offset=20", headers=_auth(_USER_A_ID))
    assert resp2.status_code == 200
    body2 = resp2.json()
    assert len(body2["repositories"]) == 5
    assert body2["pagination"]["hasMore"] is False


# ---------------------------------------------------------------------------
# 5. sort by stars_desc
# ---------------------------------------------------------------------------


async def test_list_sort_stars_desc(client: Any, db: Database) -> None:
    await _create_user(db, _USER_A_ID)
    await _create_repo(db, user_id=_USER_A_ID, stars=5)
    await _create_repo(db, user_id=_USER_A_ID, stars=100)
    await _create_repo(db, user_id=_USER_A_ID, stars=42)

    resp = client.get("/v1/repositories?sort=stars_desc", headers=_auth(_USER_A_ID))
    assert resp.status_code == 200
    stars = [r["stars"] for r in resp.json()["repositories"]]
    assert stars == sorted(stars, reverse=True)


# ---------------------------------------------------------------------------
# 6. detail returns full payload
# ---------------------------------------------------------------------------


async def test_get_detail_returns_full_payload(client: Any, db: Database) -> None:
    await _create_user(db, _USER_A_ID)
    analysis = {
        "purpose": "A test repository for unit testing purposes only.",
        "tech_stack": ["Python"],
        "architecture_summary": "Simple architecture with a single module and no dependencies.",
        "key_concepts": [{"term": "test", "explanation": "A unit test concept."}],
        "code_patterns": [],
        "use_cases": ["Testing"],
        "target_audience": "Developers",
        "maturity": "stable",
        "key_dependencies": [],
        "hallucination_risk": "low",
        "confidence": 0.9,
    }
    repo = await _create_repo(db, user_id=_USER_A_ID, analysis_json=analysis)

    resp = client.get(f"/v1/repositories/{repo.id}", headers=_auth(_USER_A_ID))
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == repo.id
    assert data["has_analysis"] is True
    assert data["analysis"]["confidence"] == 0.9


# ---------------------------------------------------------------------------
# 7. detail 404 for another user's repo
# ---------------------------------------------------------------------------


async def test_get_detail_404_for_other_users_repo(client: Any, db: Database) -> None:
    await _create_user(db, _USER_A_ID)
    await _create_user(db, _USER_B_ID)
    repo = await _create_repo(db, user_id=_USER_B_ID)

    resp = client.get(f"/v1/repositories/{repo.id}", headers=_auth(_USER_A_ID))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 8. ingest invalid URL → 400
# ---------------------------------------------------------------------------


def test_post_ingest_invalid_url_returns_400(client: Any, db: Database) -> None:
    from app.api.main import app
    from app.api.routers.repositories import _get_github_extractor

    app.dependency_overrides[_get_github_extractor] = lambda: MagicMock()
    try:
        resp = client.post(
            "/v1/repositories",
            json={"url": "https://example.com/not-a-github-url"},
            headers=_auth(_USER_A_ID),
        )
    finally:
        app.dependency_overrides.pop(_get_github_extractor, None)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 9. ingest happy path (mocked extractor)
# ---------------------------------------------------------------------------


async def test_post_ingest_happy_path(client: Any, db: Database) -> None:
    await _create_user(db, _USER_A_ID)

    mock_result = MagicMock()
    mock_result.request_id = 42
    mock_result.title = "octocat/hello-world"
    mock_result.metadata = {"full_name": "octocat/hello-world"}

    mock_extractor = MagicMock()
    mock_extractor.extract = AsyncMock(return_value=mock_result)

    from app.api.main import app
    from app.api.routers.repositories import _get_github_extractor

    app.dependency_overrides[_get_github_extractor] = lambda: mock_extractor
    try:
        resp = client.post(
            "/v1/repositories",
            json={"url": "https://github.com/octocat/hello-world"},
            headers=_auth(_USER_A_ID),
        )
    finally:
        app.dependency_overrides.pop(_get_github_extractor, None)

    assert resp.status_code == 202
    data = resp.json()
    assert data["repository_id"] == 42
    assert data["full_name"] == "octocat/hello-world"
    assert data["status"] == "ready"


# ---------------------------------------------------------------------------
# 10. reanalyze calls use case with force=True
# ---------------------------------------------------------------------------


async def test_post_reanalyze_calls_use_case_with_force_true(client: Any, db: Database) -> None:
    await _create_user(db, _USER_A_ID)
    repo = await _create_repo(db, user_id=_USER_A_ID)

    mock_use_case = MagicMock()

    async def _fake_analyze(repo_id: int, *, force: bool, correlation_id: str, **_: Any) -> Any:
        assert force is True
        assert repo_id == repo.id
        return MagicMock(analysis=None, cached=False, embedding_refreshed=False)

    mock_use_case.analyze = _fake_analyze

    from app.api.main import app
    from app.api.routers.repositories import _get_analyze_use_case

    app.dependency_overrides[_get_analyze_use_case] = lambda: mock_use_case
    try:
        resp = client.post(
            f"/v1/repositories/{repo.id}/reanalyze",
            headers=_auth(_USER_A_ID),
        )
    finally:
        app.dependency_overrides.pop(_get_analyze_use_case, None)

    # 200 with full repository detail
    assert resp.status_code == 200
    assert resp.json()["id"] == repo.id


async def test_post_reanalyze_404_for_other_users_repo(client: Any, db: Database) -> None:
    await _create_user(db, _USER_A_ID)
    await _create_user(db, _USER_B_ID)
    repo = await _create_repo(db, user_id=_USER_B_ID)

    mock_use_case = MagicMock()
    mock_use_case.analyze = AsyncMock()

    from app.api.main import app
    from app.api.routers.repositories import _get_analyze_use_case

    app.dependency_overrides[_get_analyze_use_case] = lambda: mock_use_case
    try:
        resp = client.post(
            f"/v1/repositories/{repo.id}/reanalyze",
            headers=_auth(_USER_A_ID),
        )
    finally:
        app.dependency_overrides.pop(_get_analyze_use_case, None)

    assert resp.status_code == 404
    mock_use_case.analyze.assert_not_awaited()


# ---------------------------------------------------------------------------
# 11. delete removes repo and calls Qdrant
# ---------------------------------------------------------------------------


async def test_delete_removes_repo_and_qdrant_point(client: Any, db: Database) -> None:
    from sqlalchemy import select

    await _create_user(db, _USER_A_ID)
    repo = await _create_repo(db, user_id=_USER_A_ID)

    mock_qdrant = MagicMock()
    mock_qdrant.available = True
    mock_qdrant._environment = "test"
    mock_qdrant._user_scope = "default"
    mock_qdrant._client = MagicMock()
    mock_qdrant._client.delete = MagicMock()
    mock_qdrant._collection_name = "embeddings"

    from app.api.main import app
    from app.api.routers.repositories import _get_qdrant

    app.dependency_overrides[_get_qdrant] = lambda: mock_qdrant
    try:
        resp = client.delete(
            f"/v1/repositories/{repo.id}",
            headers=_auth(_USER_A_ID),
        )
    finally:
        app.dependency_overrides.pop(_get_qdrant, None)

    assert resp.status_code == 204

    # Row is gone from Postgres
    async with db.session() as session:
        gone = await session.scalar(select(Repository).where(Repository.id == repo.id))
    assert gone is None


# ---------------------------------------------------------------------------
# 12. delete 404 for another user's repo (no leakage)
# ---------------------------------------------------------------------------


async def test_delete_404_for_other_users_repo(client: Any, db: Database) -> None:
    await _create_user(db, _USER_A_ID)
    await _create_user(db, _USER_B_ID)
    repo = await _create_repo(db, user_id=_USER_B_ID)

    from app.api.main import app
    from app.api.routers.repositories import _get_qdrant

    app.dependency_overrides[_get_qdrant] = lambda: None
    try:
        resp = client.delete(f"/v1/repositories/{repo.id}", headers=_auth(_USER_A_ID))
    finally:
        app.dependency_overrides.pop(_get_qdrant, None)
    assert resp.status_code == 404
