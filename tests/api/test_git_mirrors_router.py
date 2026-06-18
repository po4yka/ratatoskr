"""Hermetic tests for app.api.routers.git_mirrors.

Covers the uncovered paths identified in the coverage report:
- _get_db / _get_app_config / _get_git_backup_config / _get_mirror_repo /
  _get_correlation_id dependency providers
- _mirror_to_compact and _mirror_to_detail formatters (enum .value and str fallbacks)
- _load_owned_mirror helper (found / not-found)
- list_mirrors endpoint (count + rows query, pagination math, has_more)
- register_mirror endpoint (github vs manual source classification,
  upsert_target success, 500 exception path)
- get_mirror endpoint (404 path, 200 detail response)
- search_mirrors endpoint (success path, exception fallback empty response)
- delete_mirror endpoint (404 path, DB delete, Qdrant point deletion best-effort,
  on-disk rmtree path-safety check and unsafe-path warning)

All tests are hermetic: no Postgres, no Qdrant, no network.
Fakes use MagicMock / AsyncMock context managers mirroring the patterns in
tests/adapters/git_backup/test_git_mirror_readme_indexer.py.

Import strategy: the module is loaded once at module level via importlib,
bypassing `app.api.routers.__init__`, which would trigger the full router
package import chain (health -> di.api -> adapters.transcription -> numpy) and
cause "cannot load module more than once per process" when pytest loads
tests/api/conftest.py first.
"""

from __future__ import annotations

import importlib
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Load the module directly, not via the package __init__.
_gm = importlib.import_module("app.api.routers.git_mirrors")


# ---------------------------------------------------------------------------
# Fake DB helpers (mirror the pattern from test_git_mirror_readme_indexer.py)
# ---------------------------------------------------------------------------


class _Ctx:
    """Async context manager that yields a fake session."""

    def __init__(self, session: Any) -> None:
        self._s = session

    async def __aenter__(self) -> Any:
        return self._s

    async def __aexit__(self, *_args: Any) -> bool:
        return False


# ---------------------------------------------------------------------------
# Fake GitMirror row builder
# ---------------------------------------------------------------------------

_NOW = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _make_mirror_row(
    *,
    mirror_id: int = 7,
    user_id: int = 42,
    clone_url: str = "https://example.com/repo.git",
    name: str = "my-repo",
    mirror_path: str = "/data/git/my-repo.git",
    status: Any = None,
    source: Any = None,
    repository_id: int | None = None,
) -> MagicMock:
    """Build a fake GitMirror ORM row."""
    row = MagicMock()
    row.id = mirror_id
    row.user_id = user_id
    row.clone_url = clone_url
    row.name = name
    row.mirror_path = mirror_path
    row.repository_id = repository_id
    row.last_mirrored_at = _NOW
    row.size_kb = 1024
    row.default_branch = "main"
    row.consecutive_failures = 0
    row.last_error = None
    row.last_error_category = None
    row.backoff_until = None
    row.last_attempt_at = None
    row.created_at = _NOW
    row.updated_at = _NOW

    # Provide enum-like status/source with .value
    if status is None:
        s = MagicMock()
        s.value = "pending"
        row.status = s
    else:
        row.status = status

    if source is None:
        src = MagicMock()
        src.value = "manual"
        row.source = src
    else:
        row.source = source

    return row


def _make_repository_row(
    *,
    repository_id: int = 123,
    user_id: int = 42,
    full_name: str = "owner/repo",
) -> MagicMock:
    row = MagicMock()
    row.id = repository_id
    row.user_id = user_id
    row.full_name = full_name
    row.url = f"https://github.com/{full_name}"
    return row


# ===========================================================================
# Dependency provider tests
# ===========================================================================


def test_get_correlation_id_from_state() -> None:
    """_get_correlation_id uses request.state.correlation_id when present."""
    request = MagicMock()
    request.state.correlation_id = "test-cid-123"
    assert _gm._get_correlation_id(request) == "test-cid-123"


def test_get_correlation_id_fallback_uuid() -> None:
    """_get_correlation_id generates a UUID when correlation_id is absent."""
    request = MagicMock()
    # Simulate missing attribute: getattr returns None
    type(request.state).correlation_id = property(lambda self: None)

    cid = _gm._get_correlation_id(request)
    # Must be a parseable UUID
    uuid.UUID(cid)


def test_get_db_delegates_to_session_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_db calls get_session_manager with the request object."""
    fake_db = MagicMock()

    def _fake_get_session_manager(req: Any) -> Any:
        return fake_db

    monkeypatch.setattr(
        "app.api.dependencies.database.get_session_manager",
        _fake_get_session_manager,
    )

    request = MagicMock()
    result = _gm._get_db(request)
    assert result is fake_db


def test_get_app_config_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_app_config extracts cfg from the api runtime."""
    fake_cfg = MagicMock()
    fake_runtime = MagicMock()
    fake_runtime.cfg = fake_cfg

    monkeypatch.setattr(
        "app.di.api.resolve_api_runtime",
        lambda req: fake_runtime,
    )

    request = MagicMock()
    result = _gm._get_app_config(request)
    assert result is fake_cfg


def test_get_git_backup_config_returns_git_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_git_backup_config returns cfg.git_backup."""
    fake_git_backup_cfg = MagicMock()
    fake_cfg = MagicMock()
    fake_cfg.git_backup = fake_git_backup_cfg
    fake_runtime = MagicMock()
    fake_runtime.cfg = fake_cfg

    monkeypatch.setattr(
        "app.di.api.resolve_api_runtime",
        lambda req: fake_runtime,
    )

    request = MagicMock()
    result = _gm._get_git_backup_config(request)
    assert result is fake_git_backup_cfg


def test_get_mirror_repo_constructs_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """_get_mirror_repo constructs a GitMirrorRepository with db + config."""
    fake_db = MagicMock()
    fake_git_backup_cfg = MagicMock()
    fake_cfg = MagicMock()
    fake_cfg.git_backup = fake_git_backup_cfg
    fake_runtime = MagicMock()
    fake_runtime.cfg = fake_cfg

    monkeypatch.setattr(
        "app.api.dependencies.database.get_session_manager",
        lambda req: fake_db,
    )
    monkeypatch.setattr(
        "app.di.api.resolve_api_runtime",
        lambda req: fake_runtime,
    )

    constructed_args: list[dict[str, Any]] = []

    class _FakeRepo:
        def __init__(self, **kwargs: Any) -> None:
            constructed_args.append(kwargs)

    monkeypatch.setattr(
        "app.adapters.git_backup.repository.GitMirrorRepository",
        _FakeRepo,
    )

    request = MagicMock()
    result = _gm._get_mirror_repo(request)
    assert isinstance(result, _FakeRepo)
    assert constructed_args[0]["db"] is fake_db
    assert constructed_args[0]["config"] is fake_git_backup_cfg


# ===========================================================================
# Formatter tests
# ===========================================================================


def test_mirror_to_compact_with_enum_values() -> None:
    """_mirror_to_compact uses .value for status and source when they have it."""
    row = _make_mirror_row(mirror_id=1)
    compact = _gm._mirror_to_compact(row)

    assert compact.id == 1
    assert compact.clone_url == "https://example.com/repo.git"
    assert compact.name == "my-repo"
    assert compact.status == "pending"
    assert compact.source == "manual"
    assert compact.last_mirrored_at == _NOW
    assert compact.size_kb == 1024
    assert compact.repository_id is None


def test_mirror_to_compact_with_plain_string_status_source() -> None:
    """_mirror_to_compact falls back to str() when status/source lack .value."""
    row = _make_mirror_row(mirror_id=2, status="active", source="github")
    compact = _gm._mirror_to_compact(row)

    assert compact.status == "active"
    assert compact.source == "github"


def test_mirror_to_detail_all_fields() -> None:
    """_mirror_to_detail includes all detail-only fields."""
    row = _make_mirror_row(mirror_id=5)
    detail = _gm._mirror_to_detail(row)

    assert detail.id == 5
    assert detail.mirror_path == "/data/git/my-repo.git"
    assert detail.default_branch == "main"
    assert detail.consecutive_failures == 0
    assert detail.last_error is None
    assert detail.created_at == _NOW
    assert detail.updated_at == _NOW


# ===========================================================================
# _load_owned_mirror tests
# ===========================================================================


@pytest.mark.asyncio
async def test_load_owned_mirror_returns_row_when_found() -> None:
    """_load_owned_mirror returns the ORM row when it belongs to the user."""
    row = _make_mirror_row(mirror_id=10, user_id=99)

    session = MagicMock()

    async def _execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=row)
        return result

    session.execute = _execute

    db = MagicMock()
    db.session.return_value = _Ctx(session)

    result = await _gm._load_owned_mirror(db, mirror_id=10, user_id=99)
    assert result is row


@pytest.mark.asyncio
async def test_load_owned_mirror_returns_none_when_not_found() -> None:
    """_load_owned_mirror returns None when no matching row exists."""
    session = MagicMock()

    async def _execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    session.execute = _execute

    db = MagicMock()
    db.session.return_value = _Ctx(session)

    result = await _gm._load_owned_mirror(db, mirror_id=999, user_id=1)
    assert result is None


@pytest.mark.asyncio
async def test_load_owned_repository_filters_by_user() -> None:
    """_load_owned_repository includes both repository id and authenticated user id."""
    captured: dict[str, Any] = {}
    row = _make_repository_row(repository_id=123, user_id=99)

    session = MagicMock()

    async def _execute(stmt: Any) -> MagicMock:
        captured["stmt"] = stmt
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=row)
        return result

    session.execute = _execute
    db = MagicMock()
    db.session.return_value = _Ctx(session)

    result = await _gm._load_owned_repository(db, repository_id=123, user_id=99)

    assert result is row
    statement_text = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True}))
    assert "repositories.id = 123" in statement_text
    assert "repositories.user_id = 99" in statement_text


# ===========================================================================
# list_mirrors endpoint tests
# ===========================================================================


@pytest.mark.asyncio
async def test_list_mirrors_returns_compact_list() -> None:
    """list_mirrors returns mirrors + pagination from DB queries."""
    row = _make_mirror_row(mirror_id=3, user_id=1)

    session = MagicMock()

    async def _scalar(_stmt: Any) -> int:
        return 1

    async def _execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]
        return result

    session.scalar = _scalar
    session.execute = _execute

    db = MagicMock()
    db.session.return_value = _Ctx(session)

    user = {"user_id": 1}
    response = await _gm.list_mirrors(limit=20, offset=0, user=user, db=db)

    assert len(response.mirrors) == 1
    assert response.mirrors[0].id == 3
    assert response.pagination.total == 1
    assert response.pagination.has_more is False


@pytest.mark.asyncio
async def test_list_mirrors_has_more_when_more_rows() -> None:
    """has_more is True when offset + len(rows) < total."""
    row = _make_mirror_row(mirror_id=1, user_id=1)

    session = MagicMock()

    async def _scalar(_stmt: Any) -> int:
        return 5  # total = 5

    async def _execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalars.return_value.all.return_value = [row]  # 1 row
        return result

    session.scalar = _scalar
    session.execute = _execute

    db = MagicMock()
    db.session.return_value = _Ctx(session)

    user = {"user_id": 1}
    # offset=0, 1 row returned, total=5 -> has_more = (0+1) < 5 = True
    response = await _gm.list_mirrors(limit=2, offset=0, user=user, db=db)

    assert response.pagination.total == 5
    assert response.pagination.has_more is True


@pytest.mark.asyncio
async def test_list_mirrors_empty_result() -> None:
    """list_mirrors handles zero rows gracefully."""
    session = MagicMock()

    async def _scalar(_stmt: Any) -> int:
        return 0

    async def _execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    session.scalar = _scalar
    session.execute = _execute

    db = MagicMock()
    db.session.return_value = _Ctx(session)

    user = {"user_id": 1}
    response = await _gm.list_mirrors(limit=20, offset=0, user=user, db=db)

    assert response.mirrors == []
    assert response.pagination.total == 0
    assert response.pagination.has_more is False


# ===========================================================================
# register_mirror endpoint tests
# ===========================================================================


@pytest.mark.asyncio
async def test_register_mirror_github_url_classifies_as_github() -> None:
    """register_mirror classifies github.com URL as GITHUB source."""
    from app.db.models.git_backup import GitMirrorSource

    captured_calls: list[dict[str, Any]] = []

    async def _fake_upsert(**kwargs: Any) -> MagicMock:
        captured_calls.append(kwargs)
        row = MagicMock()
        row.id = 42
        s = MagicMock()
        s.value = "pending"
        row.status = s
        row.clone_url = kwargs["clone_url"]
        return row

    mirror_repo = MagicMock()
    mirror_repo.upsert_target = _fake_upsert

    from app.api.models.requests import RegisterMirrorRequest

    body = RegisterMirrorRequest(
        clone_url="https://github.com/user/repo.git",
        name="repo",
        repository_id=None,
    )
    user = {"user_id": 10}

    response = await _gm.register_mirror(
        body=body,
        user=user,
        mirror_repo=mirror_repo,
        correlation_id="cid-001",
    )

    assert len(captured_calls) == 1
    assert captured_calls[0]["source"] == GitMirrorSource.GITHUB
    assert response.id == 42
    assert response.status == "pending"


@pytest.mark.asyncio
async def test_register_mirror_non_github_url_classifies_as_manual() -> None:
    """register_mirror classifies non-GitHub URL as MANUAL source."""
    from app.db.models.git_backup import GitMirrorSource

    captured_calls: list[dict[str, Any]] = []

    async def _fake_upsert(**kwargs: Any) -> MagicMock:
        captured_calls.append(kwargs)
        row = MagicMock()
        row.id = 55
        s = MagicMock()
        s.value = "pending"
        row.status = s
        row.clone_url = kwargs["clone_url"]
        return row

    mirror_repo = MagicMock()
    mirror_repo.upsert_target = _fake_upsert

    from app.api.models.requests import RegisterMirrorRequest

    body = RegisterMirrorRequest(
        clone_url="https://gitlab.com/user/repo.git",
        name="gl-repo",
        repository_id=None,
    )
    user = {"user_id": 10}

    response = await _gm.register_mirror(
        body=body,
        user=user,
        mirror_repo=mirror_repo,
        correlation_id="cid-002",
    )

    assert captured_calls[0]["source"] == GitMirrorSource.MANUAL
    assert response.id == 55


@pytest.mark.asyncio
async def test_register_mirror_raises_500_on_upsert_failure() -> None:
    """register_mirror converts upsert exceptions to HTTP 500."""
    from fastapi import HTTPException

    async def _failing_upsert(**kwargs: Any) -> None:
        raise RuntimeError("DB connection lost")

    mirror_repo = MagicMock()
    mirror_repo.upsert_target = _failing_upsert

    from app.api.models.requests import RegisterMirrorRequest

    body = RegisterMirrorRequest(
        clone_url="https://example.com/repo.git",
        name="repo",
        repository_id=None,
    )
    user = {"user_id": 10}

    with pytest.raises(HTTPException) as exc_info:
        await _gm.register_mirror(
            body=body,
            user=user,
            mirror_repo=mirror_repo,
            correlation_id="cid-err",
        )

    assert exc_info.value.status_code == 500
    assert "cid-err" in exc_info.value.detail


@pytest.mark.asyncio
async def test_register_mirror_status_plain_string_fallback() -> None:
    """register_mirror works when row.status has no .value attribute."""

    async def _fake_upsert(**kwargs: Any) -> MagicMock:
        row = MagicMock()
        row.id = 7
        row.status = "cloning"  # plain string, no .value
        row.clone_url = kwargs["clone_url"]
        return row

    mirror_repo = MagicMock()
    mirror_repo.upsert_target = _fake_upsert

    from app.api.models.requests import RegisterMirrorRequest

    body = RegisterMirrorRequest(
        clone_url="https://gitlab.com/user/myrepo.git",
        name=None,
        repository_id=None,
    )
    user = {"user_id": 5}

    response = await _gm.register_mirror(
        body=body,
        user=user,
        mirror_repo=mirror_repo,
        correlation_id="cid-003",
    )

    assert response.status == "cloning"


@pytest.mark.asyncio
async def test_register_mirror_rejects_foreign_repository_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """register_mirror rejects repository_id values not owned by the user."""
    from fastapi import HTTPException

    async def _missing_repository(*args: Any, **kwargs: Any) -> None:
        pass

    monkeypatch.setattr(_gm, "_load_owned_repository", _missing_repository)

    from app.api.models.requests import RegisterMirrorRequest

    body = RegisterMirrorRequest(
        clone_url="https://github.com/owner/repo.git",
        name="repo",
        repository_id=123,
    )

    mirror_repo = MagicMock()
    mirror_repo.upsert_target = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await _gm.register_mirror(
            body=body,
            user={"user_id": 10},
            mirror_repo=mirror_repo,
            db=MagicMock(),
            correlation_id="cid-repo",
        )

    assert exc_info.value.status_code == 404
    mirror_repo.upsert_target.assert_not_called()


@pytest.mark.asyncio
async def test_register_mirror_rejects_repository_clone_url_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """register_mirror only links repository_id to its matching GitHub clone URL."""
    from fastapi import HTTPException

    async def _owned_repository(*args: Any, **kwargs: Any) -> MagicMock:
        return _make_repository_row(repository_id=123, user_id=10, full_name="owner/repo")

    monkeypatch.setattr(_gm, "_load_owned_repository", _owned_repository)

    from app.api.models.requests import RegisterMirrorRequest

    body = RegisterMirrorRequest(
        clone_url="https://github.com/other/repo.git",
        name="repo",
        repository_id=123,
    )

    mirror_repo = MagicMock()
    mirror_repo.upsert_target = AsyncMock()

    with pytest.raises(HTTPException) as exc_info:
        await _gm.register_mirror(
            body=body,
            user={"user_id": 10},
            mirror_repo=mirror_repo,
            db=MagicMock(),
            correlation_id="cid-mismatch",
        )

    assert exc_info.value.status_code == 400
    mirror_repo.upsert_target.assert_not_called()


@pytest.mark.asyncio
async def test_register_mirror_accepts_owned_matching_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """register_mirror preserves repository_id when the owned repository matches clone_url."""
    captured_calls: list[dict[str, Any]] = []

    async def _owned_repository(*args: Any, **kwargs: Any) -> MagicMock:
        return _make_repository_row(repository_id=123, user_id=10, full_name="owner/repo")

    async def _fake_upsert(**kwargs: Any) -> MagicMock:
        captured_calls.append(kwargs)
        row = MagicMock()
        row.id = 1234
        row.status.value = "pending"
        row.clone_url = kwargs["clone_url"]
        return row

    monkeypatch.setattr(_gm, "_load_owned_repository", _owned_repository)

    from app.api.models.requests import RegisterMirrorRequest

    body = RegisterMirrorRequest(
        clone_url="https://github.com/owner/repo.git",
        name="repo",
        repository_id=123,
    )

    mirror_repo = MagicMock()
    mirror_repo.upsert_target = _fake_upsert

    response = await _gm.register_mirror(
        body=body,
        user={"user_id": 10},
        mirror_repo=mirror_repo,
        db=MagicMock(),
        correlation_id="cid-owned",
    )

    assert response.id == 1234
    assert captured_calls[0]["repository_id"] == 123


# ===========================================================================
# get_mirror endpoint tests
# ===========================================================================


@pytest.mark.asyncio
async def test_get_mirror_returns_404_when_not_found() -> None:
    """get_mirror raises HTTP 404 when the mirror does not belong to the user."""
    from fastapi import HTTPException

    session = MagicMock()

    async def _execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    session.execute = _execute
    db = MagicMock()
    db.session.return_value = _Ctx(session)

    user = {"user_id": 1}

    with pytest.raises(HTTPException) as exc_info:
        await _gm.get_mirror(mirror_id=999, user=user, db=db)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_mirror_returns_detail_when_found() -> None:
    """get_mirror returns GitMirrorDetail for an owned mirror."""
    row = _make_mirror_row(mirror_id=20, user_id=1)

    session = MagicMock()

    async def _execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=row)
        return result

    session.execute = _execute
    db = MagicMock()
    db.session.return_value = _Ctx(session)

    user = {"user_id": 1}
    detail = await _gm.get_mirror(mirror_id=20, user=user, db=db)

    assert detail.id == 20
    assert detail.clone_url == "https://example.com/repo.git"
    assert detail.mirror_path == "/data/git/my-repo.git"


# ===========================================================================
# search_mirrors endpoint tests
# ===========================================================================


@pytest.mark.asyncio
async def test_search_mirrors_returns_items_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search_mirrors builds GitMirrorSearchResponse from service results."""
    # Fake search result item
    fake_item = MagicMock()
    fake_item.mirror_id = 11
    fake_item.clone_url = "https://example.com/r.git"
    fake_item.name = "r"
    fake_item.status = "ok"
    fake_item.source = "manual"
    fake_item.last_mirrored_at = None
    fake_item.size_kb = None
    fake_item.repository_id = None
    fake_item.distance = 0.1

    fake_results = MagicMock()
    fake_results.items = [fake_item]
    fake_results.total = 1
    fake_results.limit = 20

    class _FakeSearchService:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def search(self, q: str, *, user_id: int, limit: int, correlation_id: Any) -> Any:
            return fake_results

    fake_cfg = MagicMock()
    fake_cfg.embedding = MagicMock()
    fake_cfg.vector_store = MagicMock()
    fake_cfg.vector_store.environment = "prod"
    fake_cfg.vector_store.user_scope = "owner"

    # Patch all three imports made inside the try block
    monkeypatch.setattr(
        "app.infrastructure.embedding.embedding_factory.create_embedding_service",
        lambda cfg: MagicMock(),
    )
    monkeypatch.setattr(
        "app.di.shared.build_qdrant_vector_store",
        lambda cfg: MagicMock(),
    )
    monkeypatch.setattr(
        "app.infrastructure.search.git_mirror_search_service.GitMirrorSearchService",
        _FakeSearchService,
    )
    # Patch _get_app_config on the module object directly
    monkeypatch.setattr(_gm, "_get_app_config", lambda req: fake_cfg)

    request = MagicMock()
    request.state.correlation_id = "cid-search"

    session = MagicMock()

    async def _execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    session.execute = _execute
    db = MagicMock()
    db.session.return_value = _Ctx(session)

    user = {"user_id": 1}
    response = await _gm.search_mirrors(request=request, q="find repo", limit=20, user=user, db=db)

    assert response.total == 1
    assert len(response.items) == 1
    assert response.items[0].mirror_id == 11
    assert response.items[0].distance == 0.1


@pytest.mark.asyncio
async def test_search_mirrors_returns_empty_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """search_mirrors returns empty response when the search pipeline raises."""

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("Qdrant unreachable")

    monkeypatch.setattr(
        "app.infrastructure.embedding.embedding_factory.create_embedding_service",
        _boom,
    )

    fake_cfg = MagicMock()
    fake_cfg.embedding = MagicMock()
    fake_cfg.vector_store = MagicMock()

    monkeypatch.setattr(_gm, "_get_app_config", lambda req: fake_cfg)

    request = MagicMock()
    request.state.correlation_id = "cid-err"

    db = MagicMock()
    db.session.return_value = _Ctx(MagicMock())

    user = {"user_id": 1}
    response = await _gm.search_mirrors(request=request, q="anything", limit=10, user=user, db=db)

    assert response.items == []
    assert response.total == 0
    assert response.limit == 10


# ===========================================================================
# delete_mirror endpoint tests
# ===========================================================================


@pytest.mark.asyncio
async def test_delete_mirror_returns_404_when_not_found() -> None:
    """delete_mirror raises HTTP 404 when mirror does not belong to the user."""
    from fastapi import HTTPException

    session = MagicMock()

    async def _execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        return result

    session.execute = _execute
    db = MagicMock()
    db.session.return_value = _Ctx(session)
    db.transaction.return_value = _Ctx(AsyncMock())

    request = MagicMock()
    user = {"user_id": 1}

    with pytest.raises(HTTPException) as exc_info:
        await _gm.delete_mirror(request=request, mirror_id=999, user=user, db=db)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_mirror_deletes_db_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """delete_mirror executes a DELETE statement for the mirror row."""
    mirror_dir = tmp_path / "repo.git"
    mirror_dir.mkdir()

    row = _make_mirror_row(mirror_id=33, user_id=5, mirror_path=str(mirror_dir))

    load_session = MagicMock()

    async def _load_execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=row)
        return result

    load_session.execute = _load_execute
    db = MagicMock()
    db.session.return_value = _Ctx(load_session)

    delete_session = AsyncMock()
    delete_session.execute = AsyncMock(return_value=MagicMock())
    db.transaction.return_value = _Ctx(delete_session)

    fake_cfg = MagicMock()
    fake_cfg.git_backup.data_path = str(tmp_path)
    fake_cfg.vector_store.environment = "prod"
    fake_cfg.vector_store.user_scope = "owner"
    monkeypatch.setattr(_gm, "_get_app_config", lambda req: fake_cfg)

    fake_qdrant = MagicMock()
    fake_qdrant.available = False
    monkeypatch.setattr(
        "app.di.shared.build_qdrant_vector_store",
        lambda cfg: fake_qdrant,
    )

    request = MagicMock()
    user = {"user_id": 5}

    await _gm.delete_mirror(request=request, mirror_id=33, user=user, db=db)

    assert delete_session.execute.called


@pytest.mark.asyncio
async def test_delete_mirror_removes_disk_directory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """delete_mirror removes the on-disk directory when path is safe."""
    mirror_dir = tmp_path / "safe-repo.git"
    mirror_dir.mkdir()
    assert mirror_dir.exists()

    row = _make_mirror_row(mirror_id=44, user_id=6, mirror_path=str(mirror_dir))

    load_session = MagicMock()

    async def _load_execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=row)
        return result

    load_session.execute = _load_execute
    db = MagicMock()
    db.session.return_value = _Ctx(load_session)

    delete_session = AsyncMock()
    delete_session.execute = AsyncMock(return_value=MagicMock())
    db.transaction.return_value = _Ctx(delete_session)

    fake_cfg = MagicMock()
    fake_cfg.git_backup.data_path = str(tmp_path)
    fake_cfg.vector_store.environment = "prod"
    fake_cfg.vector_store.user_scope = "owner"
    monkeypatch.setattr(_gm, "_get_app_config", lambda req: fake_cfg)

    fake_qdrant = MagicMock()
    fake_qdrant.available = False
    monkeypatch.setattr(
        "app.di.shared.build_qdrant_vector_store",
        lambda cfg: fake_qdrant,
    )

    request = MagicMock()
    user = {"user_id": 6}

    await _gm.delete_mirror(request=request, mirror_id=44, user=user, db=db)

    assert not mirror_dir.exists()


@pytest.mark.asyncio
async def test_delete_mirror_skips_unsafe_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """delete_mirror logs a warning and skips rmtree for a path outside data_root."""
    import logging

    other_dir = tmp_path / "outside"
    other_dir.mkdir()

    data_root = tmp_path / "data"
    data_root.mkdir()

    row = _make_mirror_row(mirror_id=55, user_id=7, mirror_path=str(other_dir))

    load_session = MagicMock()

    async def _load_execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=row)
        return result

    load_session.execute = _load_execute
    db = MagicMock()
    db.session.return_value = _Ctx(load_session)

    delete_session = AsyncMock()
    delete_session.execute = AsyncMock(return_value=MagicMock())
    db.transaction.return_value = _Ctx(delete_session)

    fake_cfg = MagicMock()
    fake_cfg.git_backup.data_path = str(data_root)
    fake_cfg.vector_store.environment = "prod"
    fake_cfg.vector_store.user_scope = "owner"
    monkeypatch.setattr(_gm, "_get_app_config", lambda req: fake_cfg)

    fake_qdrant = MagicMock()
    fake_qdrant.available = False
    monkeypatch.setattr(
        "app.di.shared.build_qdrant_vector_store",
        lambda cfg: fake_qdrant,
    )

    request = MagicMock()
    user = {"user_id": 7}

    with caplog.at_level(logging.WARNING, logger="app.api.routers.git_mirrors"):
        await _gm.delete_mirror(request=request, mirror_id=55, user=user, db=db)

    # Directory must still exist (rmtree was skipped)
    assert other_dir.exists()
    assert any("git_mirror_delete_disk_skipped_unsafe_path" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_delete_mirror_skips_disk_when_mirror_path_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """delete_mirror skips on-disk removal when mirror_path is empty string."""
    row = _make_mirror_row(mirror_id=66, user_id=8, mirror_path="")

    load_session = MagicMock()

    async def _load_execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=row)
        return result

    load_session.execute = _load_execute
    db = MagicMock()
    db.session.return_value = _Ctx(load_session)

    delete_session = AsyncMock()
    delete_session.execute = AsyncMock(return_value=MagicMock())
    db.transaction.return_value = _Ctx(delete_session)

    fake_cfg = MagicMock()
    fake_cfg.git_backup.data_path = str(tmp_path)
    fake_cfg.vector_store.environment = "prod"
    fake_cfg.vector_store.user_scope = "owner"
    monkeypatch.setattr(_gm, "_get_app_config", lambda req: fake_cfg)

    fake_qdrant = MagicMock()
    fake_qdrant.available = False
    monkeypatch.setattr(
        "app.di.shared.build_qdrant_vector_store",
        lambda cfg: fake_qdrant,
    )

    rmtree_calls: list[Any] = []
    with patch("shutil.rmtree", side_effect=lambda p: rmtree_calls.append(p)):
        request = MagicMock()
        user = {"user_id": 8}
        await _gm.delete_mirror(request=request, mirror_id=66, user=user, db=db)

    assert rmtree_calls == []


@pytest.mark.asyncio
async def test_delete_mirror_qdrant_error_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """Qdrant deletion failure is best-effort: DB row still deleted, no exception raised."""
    row = _make_mirror_row(mirror_id=77, user_id=9, mirror_path="")

    load_session = MagicMock()

    async def _load_execute(_stmt: Any) -> MagicMock:
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=row)
        return result

    load_session.execute = _load_execute
    db = MagicMock()
    db.session.return_value = _Ctx(load_session)

    delete_session = AsyncMock()
    delete_session.execute = AsyncMock(return_value=MagicMock())
    db.transaction.return_value = _Ctx(delete_session)

    fake_cfg = MagicMock()
    fake_cfg.git_backup.data_path = str(tmp_path)
    fake_cfg.vector_store.environment = "prod"
    fake_cfg.vector_store.user_scope = "owner"
    monkeypatch.setattr(_gm, "_get_app_config", lambda req: fake_cfg)

    def _boom_qdrant(cfg: Any) -> None:
        raise ConnectionError("Qdrant down")

    monkeypatch.setattr(
        "app.di.shared.build_qdrant_vector_store",
        _boom_qdrant,
    )

    request = MagicMock()
    user = {"user_id": 9}

    # Must complete without raising
    await _gm.delete_mirror(request=request, mirror_id=77, user=user, db=db)

    # DB delete was still executed
    assert delete_session.execute.called
