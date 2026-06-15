"""Unit tests for RepositorySearchService."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Stub out qdrant_client before any project imports touch it.
# The service imports qdrant_client.models lazily inside search(), so we
# must inject stubs before the first call, not just before the module load.
# ---------------------------------------------------------------------------


def _make_qdrant_stubs() -> None:
    """Ensure qdrant_client.models has the stub classes needed by these tests.

    The service imports qdrant_client.models lazily inside _search_body(), so
    we must guarantee the attributes exist before the first call.

    Strategy: always patch — do not bail out if the module is already loaded.
    The real qdrant_client installed in the venv may be a version that lacks
    MatchAny (or another test may have installed a partial stub first).  We
    overwrite only the specific attributes used by RepositorySearchService so
    that other tests relying on the real package (e.g. QdrantClient,
    PointIdsList) are not affected.
    """

    # Minimal stub classes that record constructor kwargs for assertions.
    class _Condition:
        def __init__(self, *, key: str, match: object) -> None:
            self.key = key
            self.match = match

    class _MatchValue:
        def __init__(self, *, value: object) -> None:
            self.value = value

    class _MatchAny:
        def __init__(self, *, any: list) -> None:
            self.any = any

    class _Filter:
        def __init__(
            self,
            *,
            must: list | None = None,
            should: list | None = None,
            min_should: object | None = None,
            must_not: list | None = None,
        ) -> None:
            self.must = must or []
            self.should = should
            self.min_should = min_should
            self.must_not = must_not

    class _MinShould:
        def __init__(self, *, conditions: list, min_count: int) -> None:
            self.conditions = conditions
            self.min_count = min_count

    class _HasIdCondition:
        def __init__(self, *, has_id: list) -> None:
            self.has_id = has_id

    # Obtain or create the qdrant_client.models module object.
    if "qdrant_client.models" in sys.modules:
        models_mod = sys.modules["qdrant_client.models"]
    else:
        models_mod = types.ModuleType("qdrant_client.models")
        sys.modules["qdrant_client.models"] = models_mod

    # Patch the attributes required by RepositorySearchService (the ones this
    # test file exercises and asserts on).  Use our custom classes so
    # assertions on constructor kwargs work correctly.
    models_mod.FieldCondition = _Condition  # type: ignore[attr-defined]
    models_mod.MatchValue = _MatchValue  # type: ignore[attr-defined]
    models_mod.MatchAny = _MatchAny  # type: ignore[attr-defined]
    models_mod.Filter = _Filter  # type: ignore[attr-defined]
    models_mod.MinShould = _MinShould  # type: ignore[attr-defined]
    models_mod.HasIdCondition = _HasIdCondition  # type: ignore[attr-defined]

    # Provide stand-ins for every other name that qdrant_store.py imports at
    # module-level.  We only need them to exist so the import succeeds when
    # qdrant_store is loaded in the same process after our stub is installed.
    # PointIdsList needs a real constructor so that `.points` holds the list
    # passed to it (used by test_delete_git_mirror_points_derives_point_ids).
    class _PointIdsList:
        def __init__(self, *, points: list) -> None:
            self.points = points

    class _FilterSelector:
        def __init__(self, **kwargs: object) -> None:
            for k, v in kwargs.items():
                setattr(self, k, v)

    if not hasattr(models_mod, "PointIdsList"):
        models_mod.PointIdsList = _PointIdsList  # type: ignore[attr-defined]
    if not hasattr(models_mod, "FilterSelector"):
        models_mod.FilterSelector = _FilterSelector  # type: ignore[attr-defined]

    for _name in (
        "Distance",
        "PayloadSchemaType",
        "PointStruct",
        "VectorParams",
    ):
        if not hasattr(models_mod, _name):
            setattr(models_mod, _name, MagicMock(name=_name))

    # Obtain or create the top-level qdrant_client module and wire .models.
    if "qdrant_client" in sys.modules:
        qdrant_mod = sys.modules["qdrant_client"]
    else:
        qdrant_mod = types.ModuleType("qdrant_client")
        sys.modules["qdrant_client"] = qdrant_mod

    qdrant_mod.models = models_mod  # type: ignore[attr-defined]

    # Ensure QdrantClient exists on the top-level module so that other test
    # modules importing qdrant_store (which does `from qdrant_client import
    # QdrantClient` at module top-level) do not fail when this stub is loaded
    # before the real package.
    if not hasattr(qdrant_mod, "QdrantClient"):
        qdrant_mod.QdrantClient = MagicMock(name="QdrantClient")  # type: ignore[attr-defined]


_make_qdrant_stubs()

# ---------------------------------------------------------------------------
# Now safe to import project code
# ---------------------------------------------------------------------------

import datetime as dt
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.infrastructure.search.repository_search_service import (
    RepositorySearchService,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_FAKE_VECTOR = [0.1] * 768


def _make_embedding_service(vector: list[float] | None = None) -> MagicMock:
    svc = MagicMock()
    svc.generate_embedding = AsyncMock(return_value=vector or _FAKE_VECTOR)
    return svc


def _make_qdrant_hit(repository_id: int, score: float) -> MagicMock:
    hit = MagicMock()
    hit.payload = {"repository_id": repository_id, "entity_type": "repository"}
    hit.score = score
    return hit


def _make_qdrant_store(hits: list[Any]) -> MagicMock:
    response = MagicMock()
    response.points = hits

    client = MagicMock()
    client.query_points.return_value = response

    store = MagicMock()
    store._client = client
    store._collection_name = "test_collection"
    return store


def _make_repo(
    id: int,
    github_id: int,
    user_id: int,
    full_name: str = "owner/repo",
    owner: str = "owner",
    name: str = "repo",
    description: str | None = None,
    primary_language: str | None = "Python",
    topics_json: list[str] | None = None,
    stars: int = 10,
    is_starred: bool = False,
    pushed_at: dt.datetime | None = None,
) -> MagicMock:
    repo = MagicMock()
    repo.id = id
    repo.github_id = github_id
    repo.user_id = user_id
    repo.full_name = full_name
    repo.owner = owner
    repo.name = name
    repo.description = description
    repo.primary_language = primary_language
    repo.topics_json = topics_json or []
    repo.stars = stars
    repo.is_starred = is_starred
    repo.pushed_at = pushed_at
    return repo


def _make_db(rows: list[Any]) -> MagicMock:
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows

    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_mock

    session = AsyncMock()
    session.execute = AsyncMock(return_value=execute_result)

    db = MagicMock()
    db.session.return_value.__aenter__ = AsyncMock(return_value=session)
    db.session.return_value.__aexit__ = AsyncMock(return_value=False)
    return db


def _make_service(
    *,
    embedding_service: Any = None,
    qdrant_store: Any = None,
    db: Any = None,
    environment: str = "test",
    user_scope: str = "private",
) -> RepositorySearchService:
    return RepositorySearchService(
        embedding_service=embedding_service or _make_embedding_service(),
        qdrant_store=qdrant_store or _make_qdrant_store([]),
        db=db or _make_db([]),
        environment=environment,
        user_scope=user_scope,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_query_raises() -> None:
    svc = _make_service()
    with pytest.raises(ValueError, match="non-empty"):
        await svc.search("  ", user_id=1)


@pytest.mark.asyncio
async def test_search_filters_by_user_id() -> None:
    """user_id must appear in the Qdrant filter's must clause."""
    hits = [_make_qdrant_hit(1, 0.9)]
    store = _make_qdrant_store(hits)
    repo = _make_repo(id=1, github_id=100, user_id=42)
    db = _make_db([repo])
    svc = _make_service(qdrant_store=store, db=db)

    await svc.search("machine learning", user_id=42)

    call_kwargs = store._client.query_points.call_args
    qdrant_filter = call_kwargs.kwargs.get("query_filter") or call_kwargs.args[2]
    must_conditions = qdrant_filter.must
    user_id_conditions = [c for c in must_conditions if hasattr(c, "key") and c.key == "user_id"]
    assert len(user_id_conditions) == 1
    assert user_id_conditions[0].match.value == 42


@pytest.mark.asyncio
async def test_search_filters_by_language() -> None:
    """Languages filter must add a primary_language MatchAny condition."""
    hits = [_make_qdrant_hit(1, 0.9)]
    store = _make_qdrant_store(hits)
    db = _make_db([_make_repo(id=1, github_id=100, user_id=1)])
    svc = _make_service(qdrant_store=store, db=db)

    await svc.search("web framework", user_id=1, languages=["Python", "Go"])

    call_kwargs = store._client.query_points.call_args
    qdrant_filter = call_kwargs.kwargs.get("query_filter") or call_kwargs.args[2]
    lang_conditions = [
        c for c in qdrant_filter.must if hasattr(c, "key") and c.key == "primary_language"
    ]
    assert len(lang_conditions) == 1
    assert set(lang_conditions[0].match.any) == {"Python", "Go"}


@pytest.mark.asyncio
async def test_search_filters_by_topics_with_should() -> None:
    """Topics filter must use should clause with min_should=1."""
    hits = [_make_qdrant_hit(1, 0.9)]
    store = _make_qdrant_store(hits)
    db = _make_db([_make_repo(id=1, github_id=100, user_id=1)])
    svc = _make_service(qdrant_store=store, db=db)

    await svc.search("testing tools", user_id=1, topics=["pytest", "testing"])

    call_kwargs = store._client.query_points.call_args
    qdrant_filter = call_kwargs.kwargs.get("query_filter") or call_kwargs.args[2]

    assert qdrant_filter.should is not None
    assert len(qdrant_filter.should) == 2
    topic_values = {c.match.value for c in qdrant_filter.should}
    assert topic_values == {"pytest", "testing"}
    assert qdrant_filter.min_should.min_count == 1
    assert qdrant_filter.min_should.conditions == qdrant_filter.should


@pytest.mark.asyncio
async def test_search_results_ordered_by_qdrant_ranking() -> None:
    """Results must follow Qdrant rank order [3, 1, 2] regardless of DB order."""
    hits = [
        _make_qdrant_hit(3, 0.95),
        _make_qdrant_hit(1, 0.85),
        _make_qdrant_hit(2, 0.75),
    ]
    store = _make_qdrant_store(hits)
    # DB returns rows in a different order
    rows = [
        _make_repo(id=2, github_id=200, user_id=1, full_name="owner/repo-2"),
        _make_repo(id=1, github_id=100, user_id=1, full_name="owner/repo-1"),
        _make_repo(id=3, github_id=300, user_id=1, full_name="owner/repo-3"),
    ]
    db = _make_db(rows)
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("data pipeline", user_id=1)

    assert len(results.items) == 3
    assert results.items[0].repository_id == 3
    assert results.items[1].repository_id == 1
    assert results.items[2].repository_id == 2


@pytest.mark.asyncio
async def test_search_returns_distance_field() -> None:
    """distance must be 1 - similarity_score and in [0, 1]."""
    hits = [_make_qdrant_hit(1, 0.8)]
    store = _make_qdrant_store(hits)
    db = _make_db([_make_repo(id=1, github_id=100, user_id=1)])
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("async programming", user_id=1)

    assert len(results.items) == 1
    item = results.items[0]
    assert item.distance == pytest.approx(0.2)
    assert 0.0 <= item.distance <= 1.0


@pytest.mark.asyncio
async def test_search_offset_and_limit_pagination() -> None:
    """offset and limit must slice the Qdrant-ordered list correctly."""
    hits = [
        _make_qdrant_hit(1, 0.99),
        _make_qdrant_hit(2, 0.95),
        _make_qdrant_hit(3, 0.90),
        _make_qdrant_hit(4, 0.85),
        _make_qdrant_hit(5, 0.80),
    ]
    store = _make_qdrant_store(hits)
    rows = [_make_repo(id=i, github_id=i * 100, user_id=1) for i in range(1, 6)]
    db = _make_db(rows)
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("python", user_id=1, limit=2, offset=1)

    assert len(results.items) == 2
    assert results.items[0].repository_id == 2
    assert results.items[1].repository_id == 3
    assert results.limit == 2
    assert results.offset == 1


@pytest.mark.asyncio
async def test_search_excludes_other_users_repos() -> None:
    """Even if Qdrant returns a repo for another user, Postgres query filters it out."""
    hits = [
        _make_qdrant_hit(10, 0.9),  # user 1's repo
        _make_qdrant_hit(20, 0.85),  # user 99's repo — must not appear
    ]
    store = _make_qdrant_store(hits)
    # DB only returns the row belonging to user_id=1 (WHERE user_id = :user_id)
    rows = [_make_repo(id=10, github_id=1000, user_id=1)]
    db = _make_db(rows)
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("kubernetes operator", user_id=1)

    assert len(results.items) == 1
    assert results.items[0].repository_id == 10
