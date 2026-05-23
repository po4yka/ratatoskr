from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api.models.responses.diagnostics import DiagnosticsResponse
from app.api.routers import admin
from app.api.routers.auth import get_current_user
from app.api.services import diagnostics_service
from app.api.services.diagnostics_service import DiagnosticsService, clear_diagnostics_cache
from app.core.time_utils import UTC
from app.infrastructure.persistence.repositories import admin_read_repository


class _FakeAdminRepo:
    async def async_diagnostics_snapshot(self, *, since, now) -> dict[str, Any]:
        return {
            "queue_backlog": {
                "by_status": {"queued": 2},
                "runnable_count": 2,
                "oldest_queued_at": now,
                "oldest_retry_after": None,
                "expired_running_leases": 0,
            },
            "llm_providers": [
                {
                    "provider": "openrouter",
                    "status": "degraded",
                    "total_count": 4,
                    "failure_count": 1,
                    "last_error_code": "error",
                    "last_error_message": "provider failed token=[REDACTED]",
                    "last_failure_at": now,
                }
            ],
            "scraper_providers": [
                {
                    "provider": "direct_html",
                    "status": "degraded",
                    "total_count": 3,
                    "failure_count": 1,
                    "last_error_code": "FETCH_FAILED",
                    "last_error_message": "fetch failed api_key=[REDACTED]",
                    "last_failure_at": now,
                }
            ],
            "integration_health": {
                "rss": {"status": "healthy", "total_count": 1, "failure_count": 0},
                "github": {"status": "degraded", "total_count": 1, "failure_count": 1},
            },
            "latest_sync_failures": [
                {
                    "source": "rss",
                    "event_id": "rss-feed:1",
                    "correlation_id": None,
                    "error_code": "RSS_FETCH_FAILED",
                    "message": "rss failed token=[REDACTED]",
                    "occurred_at": now,
                    "retryable": True,
                    "details": {},
                }
            ],
            "storage_activity": {
                "created_last_24h": {"requests": 3},
                "created_last_7d": {"requests": 7},
            },
        }


class _FakeMaintenanceService:
    def __init__(self, *, database) -> None:
        self.database = database

    async def get_db_info(self) -> dict[str, Any]:
        return {"database_size_mb": "12.5", "table_counts": {"requests": 10, "bad": -1}}


class _FakeVectorReport:
    def to_diagnostics(self) -> dict[str, Any]:
        return {
            "status": "degraded",
            "missing_embeddings": 1,
            "stale_embeddings": 0,
            "pending_embeddings": 0,
            "expected_summaries": 3,
            "expected_repositories": 1,
            "indexed_points": 2,
            "indexed_summaries": 2,
            "indexed_repositories": 0,
            "missing_summary_vectors": 1,
            "missing_repository_vectors": 1,
            "stale_embedding_model_count": 0,
            "lag_seconds": 60.0,
            "vector_store_available": True,
            "oldest_unindexed_summary_updated_at": dt.datetime(2026, 5, 22, tzinfo=UTC),
            "latest_indexed_at": dt.datetime(2026, 5, 22, 1, tzinfo=UTC),
            "details": {"scan_limit": 10},
        }


class _FakeVectorReconciler:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def inspect(self, *, now) -> _FakeVectorReport:
        return _FakeVectorReport()


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_diagnostics_cache()


def _patch_diagnostics_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.api.routers.health._check_database",
        AsyncMock(return_value={"status": "healthy", "latency_ms": 1}),
    )
    monkeypatch.setattr(
        "app.api.routers.health._check_redis", AsyncMock(return_value={"status": "healthy"})
    )
    monkeypatch.setattr(
        "app.api.routers.health._check_vector_store",
        AsyncMock(return_value={"status": "degraded", "error": "qdrant token=secret"}),
    )
    monkeypatch.setattr(
        "app.adapters.content.scraper.diagnostics.build_scraper_diagnostics",
        lambda _cfg: {
            "status": "degraded",
            "enabled": True,
            "provider_order_effective": ["direct_html"],
            "providers": {
                "direct_html": {"enabled": True, "dependency_ready": True},
                "firecrawl": {"enabled": False},
            },
        },
    )
    monkeypatch.setattr(
        "app.config.load_config",
        lambda **_kwargs: SimpleNamespace(
            embedding=SimpleNamespace(provider="local"),
            vector_reconcile=SimpleNamespace(batch_size=10),
        ),
    )
    monkeypatch.setattr(diagnostics_service, "SystemMaintenanceService", _FakeMaintenanceService)
    monkeypatch.setattr(diagnostics_service, "VectorIndexReconciler", _FakeVectorReconciler)


@pytest.mark.asyncio
async def test_diagnostics_service_response_fixture_preserves_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_diagnostics_dependencies(monkeypatch)

    response = await DiagnosticsService(
        session_manager=cast("Any", object()),
        vector_store=object(),
        admin_repo=_FakeAdminRepo(),
    ).diagnostics()

    DiagnosticsResponse.model_validate(response.model_dump())
    assert set(response.model_dump()) == {
        "generated_at",
        "cache_ttl_seconds",
        "components",
        "scraper_providers",
        "llm_providers",
        "queue_backlog",
        "vector_indexing_lag",
        "latest_sync_failures",
        "storage_growth",
    }
    assert set(response.components) == {
        "postgresql",
        "redis",
        "qdrant",
        "scraper",
        "llm",
        "rss",
        "github",
    }
    assert response.queue_backlog.by_status == {"queued": 2}
    assert response.vector_indexing_lag.missing_embeddings == 1
    assert response.storage_growth.table_counts == {"requests": 10}
    assert response.latest_sync_failures[0].source == "rss"


@pytest.mark.asyncio
async def test_diagnostics_service_redacts_health_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_diagnostics_dependencies(monkeypatch)

    response = await DiagnosticsService(
        session_manager=cast("Any", object()),
        vector_store=object(),
        admin_repo=_FakeAdminRepo(),
    ).diagnostics()

    rendered = response.model_dump_json()
    assert "qdrant token=secret" not in rendered
    assert "token=[REDACTED]" in rendered


def test_diagnostics_redaction_helpers_cover_secrets() -> None:
    message = "failed authorization=Bearer secret-token api_key=secret-key sk-123456789abc"

    service_redacted = diagnostics_service._redact_message(message)
    repo_redacted = admin_read_repository._redact_message(message)

    for redacted in (service_redacted, repo_redacted):
        assert redacted is not None
        assert "secret-token" not in redacted
        assert "secret-key" not in redacted
        assert "sk-123456789abc" not in redacted
        assert "[REDACTED]" in redacted


def test_admin_diagnostics_route_is_owner_only(monkeypatch: pytest.MonkeyPatch) -> None:
    app = FastAPI()
    app.include_router(admin.router, prefix="/v1/admin")

    current_user = {"user_id": 1}
    app.dependency_overrides[get_current_user] = lambda: current_user
    monkeypatch.setattr(admin, "_resolve_db", lambda _request: object())
    monkeypatch.setattr(admin, "_resolve_vector_store", lambda _request: object())
    monkeypatch.setattr(admin, "build_async_audit_sink", lambda _db: lambda *_args, **_kwargs: None)
    monkeypatch.setattr(admin, "record_admin_diagnostics_request", lambda *_args, **_kwargs: None)

    async def require_owner(user):
        if user["user_id"] != 1:
            raise HTTPException(status_code=403, detail="Owner permissions required")
        return {"telegram_user_id": user["user_id"], "is_owner": True}

    class _FakeDiagnosticsService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def diagnostics(self, *, request=None) -> DiagnosticsResponse:
            return DiagnosticsResponse.model_validate(
                {
                    "generated_at": dt.datetime(2026, 5, 22, tzinfo=UTC),
                    "cache_ttl_seconds": 30,
                    "components": {},
                    "scraper_providers": [],
                    "llm_providers": [],
                    "queue_backlog": {},
                    "vector_indexing_lag": {},
                    "latest_sync_failures": [],
                    "storage_growth": {},
                }
            )

    monkeypatch.setattr(admin.AuthService, "require_owner", require_owner)
    monkeypatch.setattr(admin, "DiagnosticsService", _FakeDiagnosticsService)

    operation = app.openapi()["paths"]["/v1/admin/diagnostics"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "DiagnosticsSuccessResponse"
    )

    client = TestClient(app)
    assert client.get("/v1/admin/diagnostics").status_code == 200

    current_user["user_id"] = 2
    assert client.get("/v1/admin/diagnostics").status_code == 403
