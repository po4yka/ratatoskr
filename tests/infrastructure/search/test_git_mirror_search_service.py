"""Hermetic tests for GitMirrorSearchService -- uncovered branches.

Covers the following previously-uncovered lines in git_mirror_search_service.py:
- lines 71-72:  empty / whitespace query raises ValueError
- lines 88-93:  embedding generation exception -> return empty results
- lines 126-131: Qdrant query_points exception -> return empty results
- line 143:     hit payload missing 'mirror_id' key -> skipped
- lines 146-147: hit payload mirror_id not coercible to int -> skipped
- branch 148->139: duplicate mirror_id in hits -> deduplicated
- line 153:     all hits have invalid payloads -> empty mirror_ids_ordered -> early return
- Qdrant filter construction (entity_type, user_id, environment, user_scope conditions)
- top_k = limit + 50 buffer is forwarded to query_points
- DB hydration filters out mirror rows belonging to a different user_id
- Result ordering preserved from Qdrant rank after DB hydration
- limit slicing: only first `limit` results are returned
- distance = 1 - similarity; clamped to [0, 1]
- status/source fall back to str() when .value attribute is absent
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Inject minimal qdrant_client stubs before any project imports touch it.
# The service does ``from qdrant_client.models import ...`` lazily inside
# search(), so stubs must be in sys.modules before the first call.
# ---------------------------------------------------------------------------


def _ensure_qdrant_stubs() -> None:
    """Ensure qdrant_client.models has the stub classes needed by GitMirrorSearchService.

    qdrant_store.py imports these names from qdrant_client.models at module
    top-level: Distance, FieldCondition, Filter, FilterSelector, MatchValue,
    PayloadSchemaType, PointIdsList, PointStruct, VectorParams.

    GitMirrorSearchService only uses FieldCondition, MatchValue, and Filter
    inside search(), but because qdrant_store is imported as a side-effect
    earlier in the process, all nine names must be present in whichever module
    object is registered as qdrant_client.models.

    Strategy:
    - If the real package is already in sys.modules and has a genuine
      QdrantClient (not a MagicMock), leave it untouched — it already has all
      the names.
    - Otherwise, install a minimal stub that exposes all nine model names plus
      QdrantClient, so that both qdrant_store and the search service can import
      without error regardless of collection order.
    """
    from unittest.mock import MagicMock

    # Stub classes: must behave like the real qdrant_client classes for the
    # attributes that production code and tests inspect.
    class _Condition:
        def __init__(self, *, key: str, match: object) -> None:
            self.key = key
            self.match = match

    class _MatchValue:
        def __init__(self, *, value: object) -> None:
            self.value = value

    class _Filter:
        def __init__(self, *, must: list | None = None, **_kw: Any) -> None:
            self.must = must or []

    class _PointIdsList:
        """Minimal PointIdsList stub: stores points so callers can inspect them."""

        def __init__(self, *, points: list | None = None, **_kw: Any) -> None:
            self.points = points if points is not None else []

    # All names imported from qdrant_client.models anywhere in the app that
    # may be transitively pulled in when qdrant_store or qdrant_schemas is
    # first imported in this process.  Any name not already on the module
    # gets a MagicMock so that the import succeeds without affecting the
    # behaviour under test.
    #
    # Sources:
    #   qdrant_store.py:     Distance, FieldCondition, Filter, FilterSelector,
    #                        MatchValue, PayloadSchemaType, PointIdsList,
    #                        PointStruct, VectorParams
    #   qdrant_schemas.py:   FieldCondition, Filter, MatchAny, MatchValue
    #   repository_search_service.py (lazy):
    #                        FieldCondition, Filter, MatchAny, MatchValue, MinShould
    _ALL_MODEL_NAMES: dict[str, Any] = {
        "Distance": MagicMock(name="Distance"),
        "FieldCondition": _Condition,
        "Filter": _Filter,
        "FilterSelector": MagicMock(name="FilterSelector"),
        "MatchAny": MagicMock(name="MatchAny"),
        "MatchValue": _MatchValue,
        "MinShould": MagicMock(name="MinShould"),
        "PayloadSchemaType": MagicMock(name="PayloadSchemaType"),
        "PointIdsList": _PointIdsList,
        "PointStruct": MagicMock(name="PointStruct"),
        "VectorParams": MagicMock(name="VectorParams"),
    }

    if "qdrant_client" in sys.modules:
        qdrant_mod = sys.modules["qdrant_client"]
        # Real package already loaded — it has genuine implementations of all
        # names.  Do not replace them; just return.
        if hasattr(qdrant_mod, "QdrantClient") and not isinstance(
            qdrant_mod.QdrantClient,
            MagicMock,
        ):
            return
        # Partial/stub module already in sys.modules — patch missing names in
        # place so nothing needs to be re-imported.
        if not hasattr(qdrant_mod, "QdrantClient"):
            qdrant_mod.QdrantClient = MagicMock(name="QdrantClient")  # type: ignore[attr-defined]
        models_mod = sys.modules.get("qdrant_client.models")
        if models_mod is None:
            models_mod = types.ModuleType("qdrant_client.models")
            sys.modules["qdrant_client.models"] = models_mod
        for name, obj in _ALL_MODEL_NAMES.items():
            if not hasattr(models_mod, name):
                setattr(models_mod, name, obj)
        return

    # qdrant_client not yet in sys.modules — install a complete stub.
    models_mod = types.ModuleType("qdrant_client.models")
    for name, obj in _ALL_MODEL_NAMES.items():
        setattr(models_mod, name, obj)

    qdrant_mod = types.ModuleType("qdrant_client")
    qdrant_mod.models = models_mod  # type: ignore[attr-defined]
    qdrant_mod.QdrantClient = MagicMock(name="QdrantClient")  # type: ignore[attr-defined]

    sys.modules["qdrant_client"] = qdrant_mod
    sys.modules["qdrant_client.models"] = models_mod


_ensure_qdrant_stubs()

# ---------------------------------------------------------------------------
# Now safe to import project code.
# ---------------------------------------------------------------------------

from app.infrastructure.search.git_mirror_search_service import GitMirrorSearchService

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_FAKE_VECTOR = [0.1] * 64


class _FakeEmbeddingService:
    def __init__(self, vector: list[float] | None = None, raise_on_call: bool = False) -> None:
        self._vector = vector or _FAKE_VECTOR
        self._raise = raise_on_call
        self.call_count = 0

    async def generate_embedding(self, text: str, *, language: Any, task_type: str) -> list[float]:
        self.call_count += 1
        if self._raise:
            raise RuntimeError("embedding provider offline")
        return self._vector


def _make_hit(mirror_id: Any, score: float) -> MagicMock:
    hit = MagicMock()
    hit.score = score
    hit.payload = {"mirror_id": mirror_id}
    return hit


def _make_hit_no_mirror_id(score: float = 0.8) -> MagicMock:
    hit = MagicMock()
    hit.score = score
    hit.payload = {"entity_type": "git_mirror"}  # no mirror_id key
    return hit


def _make_qdrant_store(
    hits: list[Any],
    *,
    available: bool = True,
    raise_on_query: bool = False,
) -> MagicMock:
    response = MagicMock()
    response.points = hits

    client = MagicMock()
    if raise_on_query:
        client.query_points.side_effect = ConnectionError("qdrant unreachable")
    else:
        client.query_points.return_value = response

    store = MagicMock()
    store.available = available
    store._client = client
    store._collection_name = "test_collection"
    return store


def _make_mirror(
    *,
    mirror_id: int = 1,
    user_id: int = 42,
    repository_id: int | None = None,
    clone_url: str = "https://example.com/r.git",
    name: str | None = "my-repo",
    status: str = "active",
    source: str = "gitea",
    last_mirrored_at: Any = None,
    size_kb: int | None = 100,
) -> MagicMock:
    row = MagicMock()
    row.id = mirror_id
    row.user_id = user_id
    row.repository_id = repository_id
    row.clone_url = clone_url
    row.name = name
    row.last_mirrored_at = last_mirrored_at
    row.size_kb = size_kb
    # Enum-like attribute with .value
    row.status = MagicMock()
    row.status.value = status
    row.source = MagicMock()
    row.source.value = source
    return row


def _make_db(rows: list[Any]) -> MagicMock:
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows

    execute_result = MagicMock()
    execute_result.scalars.return_value = scalars_mock

    session = AsyncMock()
    session.execute = AsyncMock(return_value=execute_result)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)

    db = MagicMock()
    db.session.return_value = ctx
    return db


def _make_service(
    *,
    embedding_service: Any = None,
    qdrant_store: Any = None,
    db: Any = None,
    environment: str = "test",
    user_scope: str = "private",
) -> GitMirrorSearchService:
    return GitMirrorSearchService(
        embedding_service=embedding_service or _FakeEmbeddingService(),
        qdrant_store=qdrant_store or _make_qdrant_store([]),
        db=db or _make_db([]),
        environment=environment,
        user_scope=user_scope,
    )


# ---------------------------------------------------------------------------
# Tests: input validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_query_raises_value_error() -> None:
    """Blank query string must raise ValueError with a descriptive message."""
    svc = _make_service()
    with pytest.raises(ValueError, match="non-empty"):
        await svc.search("", user_id=1)


@pytest.mark.asyncio
async def test_whitespace_only_query_raises_value_error() -> None:
    """All-whitespace query must also raise ValueError."""
    svc = _make_service()
    with pytest.raises(ValueError, match="non-empty"):
        await svc.search("   \t\n  ", user_id=99)


# ---------------------------------------------------------------------------
# Tests: Qdrant unavailable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qdrant_unavailable_returns_empty_results() -> None:
    store = _make_qdrant_store([], available=False)
    svc = _make_service(qdrant_store=store)

    results = await svc.search("kubernetes", user_id=7, limit=5)

    assert results.items == []
    assert results.total == 0
    assert results.limit == 5
    store._client.query_points.assert_not_called()


@pytest.mark.asyncio
async def test_qdrant_none_returns_empty_results() -> None:
    """qdrant_store=None must return empty without any attribute access."""
    svc = _make_service(qdrant_store=None)

    results = await svc.search("rust async", user_id=1, limit=10)

    assert results.items == []
    assert results.total == 0


# ---------------------------------------------------------------------------
# Tests: embedding exception path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_embedding_exception_returns_empty_results() -> None:
    """If embedding generation raises, return empty results without re-raising."""
    embedding_svc = _FakeEmbeddingService(raise_on_call=True)
    store = _make_qdrant_store([])
    svc = _make_service(embedding_service=embedding_svc, qdrant_store=store)

    results = await svc.search("database indexing", user_id=3, limit=15)

    assert results.items == []
    assert results.total == 0
    assert results.limit == 15
    assert embedding_svc.call_count == 1
    # Qdrant must not have been queried.
    store._client.query_points.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: Qdrant query exception path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_qdrant_query_exception_returns_empty_results() -> None:
    """ConnectionError from query_points must be caught and return empty results."""
    store = _make_qdrant_store([], raise_on_query=True)
    svc = _make_service(qdrant_store=store)

    results = await svc.search("ci pipeline", user_id=5, limit=8)

    assert results.items == []
    assert results.total == 0
    assert results.limit == 8


# ---------------------------------------------------------------------------
# Tests: hit payload edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hit_with_no_mirror_id_key_is_skipped() -> None:
    """Hits without a 'mirror_id' payload key must be silently skipped."""
    bad_hit = _make_hit_no_mirror_id(score=0.9)
    store = _make_qdrant_store([bad_hit])
    svc = _make_service(qdrant_store=store)

    results = await svc.search("golang", user_id=1)

    assert results.items == []
    assert results.total == 0


@pytest.mark.asyncio
async def test_hit_with_none_mirror_id_is_skipped() -> None:
    """Hits where mirror_id payload value is None must be skipped."""
    bad_hit = _make_hit(mirror_id=None, score=0.85)
    store = _make_qdrant_store([bad_hit])
    svc = _make_service(qdrant_store=store)

    results = await svc.search("elixir phoenix", user_id=1)

    assert results.items == []
    assert results.total == 0


@pytest.mark.asyncio
async def test_hit_with_non_int_mirror_id_is_skipped() -> None:
    """Hits whose mirror_id cannot be coerced to int must be skipped."""
    bad_hit = _make_hit(mirror_id="not-an-int", score=0.8)
    store = _make_qdrant_store([bad_hit])
    svc = _make_service(qdrant_store=store)

    results = await svc.search("terraform module", user_id=1)

    assert results.items == []
    assert results.total == 0


@pytest.mark.asyncio
async def test_all_hits_invalid_payloads_returns_empty() -> None:
    """When every hit has an invalid payload, mirror_ids_ordered is empty -> early return."""
    hits = [
        _make_hit(mirror_id=None, score=0.9),
        _make_hit_no_mirror_id(score=0.8),
        _make_hit(mirror_id="bad", score=0.7),
    ]
    store = _make_qdrant_store(hits)
    svc = _make_service(qdrant_store=store)

    results = await svc.search("docker swarm", user_id=2)

    assert results.items == []
    assert results.total == 0


@pytest.mark.asyncio
async def test_duplicate_mirror_ids_in_hits_are_deduplicated() -> None:
    """Qdrant may return the same mirror_id via multiple points; only the first score kept."""
    mirror_row = _make_mirror(mirror_id=7, user_id=1)
    db = _make_db([mirror_row])

    hits = [
        _make_hit(mirror_id=7, score=0.95),
        _make_hit(mirror_id=7, score=0.60),  # duplicate, lower score -- must be ignored
    ]
    store = _make_qdrant_store(hits)
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("prometheus metrics", user_id=1)

    assert len(results.items) == 1
    # Distance from first hit (0.95): 1 - 0.95 = 0.05
    assert results.items[0].distance == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Tests: Qdrant filter construction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_contains_entity_type_condition() -> None:
    """The Qdrant filter must include an entity_type='git_mirror' condition."""
    mirror_row = _make_mirror(mirror_id=1, user_id=99)
    db = _make_db([mirror_row])
    store = _make_qdrant_store([_make_hit(mirror_id=1, score=0.8)])
    svc = _make_service(qdrant_store=store, db=db)

    await svc.search("raft consensus", user_id=99)

    call_kwargs = store._client.query_points.call_args
    qdrant_filter = call_kwargs.kwargs.get("query_filter") or call_kwargs.args[2]
    entity_conditions = [
        c for c in qdrant_filter.must if hasattr(c, "key") and c.key == "entity_type"
    ]
    assert len(entity_conditions) == 1
    assert entity_conditions[0].match.value == "git_mirror"


@pytest.mark.asyncio
async def test_filter_contains_user_id_condition() -> None:
    """The Qdrant filter must scope results to the calling user_id."""
    mirror_row = _make_mirror(mirror_id=1, user_id=55)
    db = _make_db([mirror_row])
    store = _make_qdrant_store([_make_hit(mirror_id=1, score=0.75)])
    svc = _make_service(qdrant_store=store, db=db)

    await svc.search("kafka streams", user_id=55)

    call_kwargs = store._client.query_points.call_args
    qdrant_filter = call_kwargs.kwargs.get("query_filter") or call_kwargs.args[2]
    user_conditions = [c for c in qdrant_filter.must if hasattr(c, "key") and c.key == "user_id"]
    assert len(user_conditions) == 1
    assert user_conditions[0].match.value == 55


@pytest.mark.asyncio
async def test_filter_contains_environment_and_user_scope() -> None:
    """environment and user_scope conditions must appear in the Qdrant filter."""
    mirror_row = _make_mirror(mirror_id=3, user_id=10)
    db = _make_db([mirror_row])
    store = _make_qdrant_store([_make_hit(mirror_id=3, score=0.88)])
    svc = _make_service(qdrant_store=store, db=db, environment="staging", user_scope="shared")

    await svc.search("event sourcing", user_id=10)

    call_kwargs = store._client.query_points.call_args
    qdrant_filter = call_kwargs.kwargs.get("query_filter") or call_kwargs.args[2]
    env_conditions = [c for c in qdrant_filter.must if hasattr(c, "key") and c.key == "environment"]
    scope_conditions = [
        c for c in qdrant_filter.must if hasattr(c, "key") and c.key == "user_scope"
    ]
    assert len(env_conditions) == 1
    assert env_conditions[0].match.value == "staging"
    assert len(scope_conditions) == 1
    assert scope_conditions[0].match.value == "shared"


# ---------------------------------------------------------------------------
# Tests: top_k buffer forwarded to Qdrant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_top_k_is_limit_plus_50_buffer() -> None:
    """query_points must be called with limit = user_limit + 50."""
    store = _make_qdrant_store([])
    svc = _make_service(qdrant_store=store)

    await svc.search("ansible playbook", user_id=1, limit=10)

    call_kwargs = store._client.query_points.call_args
    # The 'limit' keyword arg passed to query_points is the top_k buffer.
    top_k = call_kwargs.kwargs.get("limit") or call_kwargs.args[3]
    assert top_k == 60  # 10 + 50


# ---------------------------------------------------------------------------
# Tests: DB defense-in-depth user_id check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_filters_out_wrong_user_rows() -> None:
    """Postgres WHERE user_id filters mean only matching user rows hydrate results."""
    # Qdrant returns hits for two mirror IDs but DB only returns one (correct user)
    hits = [_make_hit(mirror_id=10, score=0.9), _make_hit(mirror_id=20, score=0.8)]
    store = _make_qdrant_store(hits)
    # Simulate DB returning only mirror 10 (user 1); mirror 20 belongs to user 99.
    mirror10 = _make_mirror(mirror_id=10, user_id=1)
    db = _make_db([mirror10])
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("vault secrets", user_id=1)

    assert len(results.items) == 1
    assert results.items[0].mirror_id == 10


# ---------------------------------------------------------------------------
# Tests: ordering, limit slicing, result fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_results_ordered_by_qdrant_rank() -> None:
    """Results must follow Qdrant rank order regardless of DB row order."""
    hits = [
        _make_hit(mirror_id=3, score=0.95),
        _make_hit(mirror_id=1, score=0.85),
        _make_hit(mirror_id=2, score=0.75),
    ]
    store = _make_qdrant_store(hits)
    # DB returns rows in a shuffled order.
    rows = [
        _make_mirror(mirror_id=2, user_id=1),
        _make_mirror(mirror_id=3, user_id=1),
        _make_mirror(mirror_id=1, user_id=1),
    ]
    db = _make_db(rows)
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("service mesh", user_id=1)

    assert [r.mirror_id for r in results.items] == [3, 1, 2]


@pytest.mark.asyncio
async def test_limit_slices_results() -> None:
    """Only the first `limit` results from Qdrant-ordered list must be returned."""
    hits = [_make_hit(mirror_id=i, score=1.0 - i * 0.05) for i in range(1, 6)]
    store = _make_qdrant_store(hits)
    rows = [_make_mirror(mirror_id=i, user_id=1) for i in range(1, 6)]
    db = _make_db(rows)
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("sidecar proxy", user_id=1, limit=3)

    assert len(results.items) == 3
    assert results.limit == 3
    assert results.total == 5  # total reflects all matched, before slicing
    assert [r.mirror_id for r in results.items] == [1, 2, 3]


@pytest.mark.asyncio
async def test_distance_is_one_minus_similarity() -> None:
    """distance field must equal 1 - qdrant_score, clamped to [0, 1]."""
    hits = [_make_hit(mirror_id=1, score=0.72)]
    store = _make_qdrant_store(hits)
    db = _make_db([_make_mirror(mirror_id=1, user_id=1)])
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("grpc bidirectional", user_id=1)

    assert len(results.items) == 1
    assert results.items[0].distance == pytest.approx(0.28)
    assert 0.0 <= results.items[0].distance <= 1.0


@pytest.mark.asyncio
async def test_distance_clamped_for_score_above_one() -> None:
    """similarity score > 1.0 must be clamped: distance stays >= 0.0."""
    hits = [_make_hit(mirror_id=1, score=1.05)]
    store = _make_qdrant_store(hits)
    db = _make_db([_make_mirror(mirror_id=1, user_id=1)])
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("protocol buffers", user_id=1)

    assert results.items[0].distance >= 0.0


@pytest.mark.asyncio
async def test_result_fields_populated_correctly() -> None:
    """All GitMirrorSearchResult fields must be populated from the DB row."""
    import datetime as dt

    mirrored_at = dt.datetime(2024, 6, 1, 12, 0, 0)
    row = _make_mirror(
        mirror_id=42,
        user_id=7,
        repository_id=100,
        clone_url="https://gitea.example.com/user/repo.git",
        name="my-project",
        status="active",
        source="gitea",
        last_mirrored_at=mirrored_at,
        size_kb=512,
    )

    hits = [_make_hit(mirror_id=42, score=0.88)]
    store = _make_qdrant_store(hits)
    db = _make_db([row])
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("project query", user_id=7)

    assert len(results.items) == 1
    item = results.items[0]
    assert item.mirror_id == 42
    assert item.clone_url == "https://gitea.example.com/user/repo.git"
    assert item.name == "my-project"
    assert item.status == "active"
    assert item.source == "gitea"
    assert item.last_mirrored_at == mirrored_at
    assert item.size_kb == 512
    assert item.repository_id == 100


@pytest.mark.asyncio
async def test_status_source_fallback_when_no_value_attr() -> None:
    """When status/source lack a .value attribute, str() fallback must be used."""
    row = _make_mirror(mirror_id=5, user_id=1)
    # Replace enum-like mocks with plain strings (no .value attribute).
    row.status = "pending"
    row.source = "gitlab"

    hits = [_make_hit(mirror_id=5, score=0.6)]
    store = _make_qdrant_store(hits)
    db = _make_db([row])
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("gitlab mirror", user_id=1)

    assert len(results.items) == 1
    assert results.items[0].status == "pending"
    assert results.items[0].source == "gitlab"


@pytest.mark.asyncio
async def test_no_db_rows_returns_empty_results() -> None:
    """If Qdrant returns hits but DB hydration yields nothing, items must be empty."""
    hits = [_make_hit(mirror_id=99, score=0.8)]
    store = _make_qdrant_store(hits)
    db = _make_db([])  # DB returns no rows
    svc = _make_service(qdrant_store=store, db=db)

    results = await svc.search("unknown mirror", user_id=1)

    assert results.items == []
    assert results.total == 0


@pytest.mark.asyncio
async def test_embedding_vector_with_tolist_method() -> None:
    """Embedding objects with a .tolist() method must be converted via tolist()."""

    class NumpyLikeVector:
        def __init__(self, data: list[float]) -> None:
            self._data = data

        def tolist(self) -> list[float]:
            return self._data

    class _EmbSvc:
        async def generate_embedding(self, text: str, **_kw: Any) -> NumpyLikeVector:
            return NumpyLikeVector([0.5] * 64)

    mirror_row = _make_mirror(mirror_id=1, user_id=1)
    hits = [_make_hit(mirror_id=1, score=0.9)]
    store = _make_qdrant_store(hits)
    db = _make_db([mirror_row])
    svc = _make_service(embedding_service=_EmbSvc(), qdrant_store=store, db=db)

    results = await svc.search("numpy embedding path", user_id=1)

    assert len(results.items) == 1
    # Verify query_points was called (embedding was used successfully)
    store._client.query_points.assert_called_once()
