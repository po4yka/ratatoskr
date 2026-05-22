from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.mcp.context import McpServerContext
from app.mcp.http_auth import McpRequestIdentity


def _fake_runtime(database_dsn: str | None, user_id: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        cfg=None,
        database_dsn=database_dsn or "postgresql+asyncpg://u:p@localhost:5432/ratatoskr",
        database=SimpleNamespace(),
        scope=SimpleNamespace(user_id=user_id),
        vector_state=SimpleNamespace(
            service=None, last_failed_at=None, init_lock=None, resources=()
        ),
        local_vector_state=SimpleNamespace(
            service=None,
            last_failed_at=None,
            init_lock=None,
            resources=(),
        ),
    )


def test_init_runtime_uses_postgres_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.di.mcp as mcp_di

    captured: dict[str, Any] = {}

    def fake_build_mcp_runtime(
        *,
        database_dsn: str | None = None,
        user_id: int | None = None,
    ) -> Any:
        captured["database_dsn"] = database_dsn
        captured["user_id"] = user_id
        return _fake_runtime(database_dsn, user_id)

    monkeypatch.setattr(mcp_di, "build_mcp_runtime", fake_build_mcp_runtime)

    context = McpServerContext(
        database_dsn="postgresql+asyncpg://u:p@localhost:5432/mcp",
        user_id=42,
    )
    runtime = context.init_runtime()

    assert captured == {
        "database_dsn": "postgresql+asyncpg://u:p@localhost:5432/mcp",
        "user_id": 42,
    }
    assert runtime.database_dsn == "postgresql+asyncpg://u:p@localhost:5432/mcp"
    assert context.database_dsn == runtime.database_dsn


@pytest.mark.asyncio
async def test_get_vector_service_retries_after_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.di.mcp as mcp_di

    clock = {"now": 0.0}
    attempts = {"count": 0}

    def fake_monotonic() -> float:
        return clock["now"]

    def failing_load_config(*_args: Any, **_kwargs: Any):
        attempts["count"] += 1
        raise RuntimeError("vector store down")

    monkeypatch.setattr(mcp_di.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(mcp_di, "load_config", failing_load_config)
    monkeypatch.setattr(
        mcp_di,
        "build_mcp_runtime",
        lambda *, database_dsn=None, user_id=None: _fake_runtime(database_dsn, user_id),
    )

    context = McpServerContext(vector_retry_interval_sec=60.0)

    assert await context.init_vector_service() is None
    assert attempts["count"] == 1

    clock["now"] = 10.0
    assert await context.init_vector_service() is None
    assert attempts["count"] == 1

    clock["now"] = 61.0
    assert await context.init_vector_service() is None
    assert attempts["count"] == 2


@pytest.mark.asyncio
async def test_get_vector_service_forwards_required_and_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.di.mcp as mcp_di
    import app.infrastructure.embedding.embedding_factory as embedding_factory_module
    import app.infrastructure.search.vector_search_service as vector_service_module
    import app.infrastructure.vector.qdrant_store as qdrant_store_module

    captured: dict[str, Any] = {}

    class FakeStore:
        def __init__(self, **kwargs: Any) -> None:
            captured["store_kwargs"] = kwargs

    class FakeService:
        def __init__(self, **kwargs: Any) -> None:
            self._vector_store = kwargs["vector_store"]

    monkeypatch.setattr(
        mcp_di,
        "load_config",
        lambda *_args, **_kwargs: SimpleNamespace(
            vector_store=SimpleNamespace(
                url="http://localhost:6333",
                api_key="token",
                environment="test",
                user_scope="scope",
                collection_version="v5",
                required=True,
                connection_timeout=7.5,
            ),
            embedding=object(),
        ),
    )
    monkeypatch.setattr(embedding_factory_module, "create_embedding_service", lambda _cfg: object())
    monkeypatch.setattr(mcp_di, "resolve_embedding_space_identifier", lambda _cfg: None)
    monkeypatch.setattr(qdrant_store_module, "QdrantVectorStore", FakeStore)
    monkeypatch.setattr(vector_service_module, "StoreVectorSearchService", FakeService)
    monkeypatch.setattr(
        mcp_di,
        "build_mcp_runtime",
        lambda *, database_dsn=None, user_id=None: _fake_runtime(database_dsn, user_id),
    )

    context = McpServerContext()
    await context.init_vector_service()

    assert captured["store_kwargs"] == {
        "url": "http://localhost:6333",
        "api_key": "token",
        "environment": "test",
        "user_scope": "scope",
        "collection_version": "v5",
        "embedding_space": None,
        "required": True,
        "connection_timeout": 7.5,
    }


def test_request_user_scope_prefers_override_and_resets_to_startup_scope() -> None:
    context = McpServerContext(user_id=111)

    assert context.user_id == 111

    token = context.set_request_user_scope(222)
    try:
        assert context.user_id == 222
    finally:
        context.reset_request_user_scope(token)

    assert context.user_id == 111


def test_init_runtime_uses_startup_scope_even_with_request_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.di.mcp as mcp_di

    captured: dict[str, Any] = {}

    def fake_build_mcp_runtime(
        *,
        database_dsn: str | None = None,
        user_id: int | None = None,
    ) -> Any:
        captured["database_dsn"] = database_dsn
        captured["user_id"] = user_id
        return _fake_runtime(database_dsn, user_id)

    monkeypatch.setattr(mcp_di, "build_mcp_runtime", fake_build_mcp_runtime)

    context = McpServerContext(
        database_dsn="postgresql+asyncpg://u:p@localhost:5432/request_scope",
        user_id=111,
    )
    with context.request_user_scope(222):
        context.init_runtime()
        assert captured == {
            "database_dsn": "postgresql+asyncpg://u:p@localhost:5432/request_scope",
            "user_id": 111,
        }
        assert context.user_id == 222

    assert context.runtime.scope.user_id == 111
    assert context.user_id == 111


def test_nested_request_user_scopes_restore_without_mutating_runtime_scope() -> None:
    context = McpServerContext(user_id=111)
    context._runtime = SimpleNamespace(
        scope=SimpleNamespace(user_id=111),
        vector_state=SimpleNamespace(last_failed_at=None),
        local_vector_state=SimpleNamespace(last_failed_at=None),
    )

    with context.request_user_scope(222):
        assert context.user_id == 222
        assert context.runtime.scope.user_id == 111

        with context.request_user_scope(None):
            assert context.user_id is None
            assert context.runtime.scope.user_id == 111

        assert context.user_id == 222

    assert context.user_id == 111


def test_scope_filters_use_effective_request_user_scope() -> None:
    class _Field:
        __hash__ = object.__hash__

        def __init__(self, name: str) -> None:
            self.name = name

        def __eq__(self, other: Any) -> tuple[str, Any]:  # type: ignore[override]
            return (self.name, other)

    class _RequestModel:
        is_deleted = _Field("is_deleted")
        user_id = _Field("user_id")

    class _CollectionModel:
        is_deleted = _Field("is_deleted")
        user_id = _Field("user_id")

    context = McpServerContext(user_id=7)

    with context.request_user_scope(8):
        assert context.request_scope_filters(_RequestModel) == [
            ("is_deleted", False),
            ("user_id", 8),
        ]
        assert context.collection_scope_filters(_CollectionModel) == [
            ("is_deleted", False),
            ("user_id", 8),
        ]

    with context.request_user_scope(None):
        assert context.request_scope_filters(_RequestModel) == [("is_deleted", False)]
        assert context.collection_scope_filters(_CollectionModel) == [("is_deleted", False)]


def test_request_identity_scope_exposes_user_and_client() -> None:
    context = McpServerContext(user_id=111)
    identity = McpRequestIdentity(
        user_id=222,
        client_id="mcp-client",
        username="scoped-user",
        auth_source="authorization",
    )

    with context.request_identity_scope(identity):
        assert context.user_id == 222
        assert context.client_id == "mcp-client"
        assert context.username == "scoped-user"
        assert context.auth_source == "authorization"

    assert context.user_id == 111
    assert context.client_id is None
    assert context.username is None
    assert context.auth_source is None


def test_request_identity_scope_takes_precedence_over_request_user_override() -> None:
    context = McpServerContext(user_id=111)
    identity = McpRequestIdentity(
        user_id=222,
        client_id="mcp-client",
        username=None,
        auth_source="forwarded_bearer",
    )

    with context.request_user_scope(333):
        with context.request_identity_scope(identity):
            assert context.user_id == 222
            assert context.client_id == "mcp-client"

        assert context.user_id == 333


def test_active_mcp_request_identity_takes_precedence() -> None:
    mcp_server = pytest.importorskip("mcp.server.lowlevel.server")
    request_ctx = mcp_server.request_ctx

    context = McpServerContext(user_id=111)
    identity = McpRequestIdentity(
        user_id=444,
        client_id="mcp-public-v1",
        username="active-request",
        auth_source="authorization",
    )
    fake_request = SimpleNamespace(state=SimpleNamespace(mcp_identity=identity))
    token = request_ctx.set(cast("Any", SimpleNamespace(request=fake_request)))
    try:
        assert context.user_id == 444
        assert context.client_id == "mcp-public-v1"
        assert context.username == "active-request"
    finally:
        request_ctx.reset(token)
