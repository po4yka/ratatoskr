from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.api.models.responses.diagnostics import DiagnosticsResponse
from app.api.routers.auth.tokens import create_access_token
from app.api.services.admin_read_service import clear_diagnostics_cache
from app.core.time_utils import UTC
from app.db.models import (
    CrawlResult,
    GitHubAuthMethod,
    GitHubIntegrationStatus,
    ImportJob,
    LLMCall,
    RSSFeed,
    Request,
    RequestProcessingJob,
    Summary,
    User,
    UserGitHubIntegration,
)


def _headers(user_id: int) -> dict[str, str]:
    token = create_access_token(user_id, client_id="test")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(autouse=True)
def _clear_diagnostics_cache() -> None:
    clear_diagnostics_cache()


@pytest.mark.asyncio
async def test_admin_diagnostics_requires_owner(client: TestClient, db) -> None:
    async with db.transaction() as session:
        session.add_all(
            [
                User(telegram_user_id=6101, username="diag-owner", is_owner=True),
                User(telegram_user_id=6102, username="diag-user", is_owner=False),
            ]
        )

    forbidden = client.get("/v1/admin/diagnostics", headers=_headers(6102))
    assert forbidden.status_code == 403

    ok = client.get("/v1/admin/diagnostics", headers=_headers(6101))
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_admin_diagnostics_redacts_payloads_and_exposes_schema(
    client: TestClient, db
) -> None:
    now = dt.datetime.now(UTC)
    async with db.transaction() as session:
        owner = User(telegram_user_id=6201, username="diag-owner", is_owner=True)
        session.add(owner)
        request = Request(
            type="url",
            status="error",
            user_id=owner.telegram_user_id,
            input_url="https://example.com/private?token=raw-url-token",
            correlation_id="cid-diagnostics",
        )
        session.add(request)
        await session.flush()
        session.add_all(
            [
                LLMCall(
                    request_id=request.id,
                    provider="openrouter",
                    model="test/model",
                    status="error",
                    error_text="provider failed token=secret-token prompt: raw article body",
                    request_messages_json=[{"role": "user", "content": "raw prompt secret"}],
                    response_text="raw model output",
                ),
                CrawlResult(
                    request_id=request.id,
                    endpoint="direct_html",
                    winning_provider="direct_html",
                    firecrawl_success=False,
                    error_text="fetch failed api_key=secret-key raw html body",
                    source_url="https://example.com/private?password=secret",
                    updated_at=now,
                ),
                RequestProcessingJob(
                    request_id=request.id,
                    status="failed",
                    attempt_count=1,
                    max_attempts=3,
                    retry_after=now,
                    last_error_code="FETCH_FAILED",
                    last_error_message="url failed authorization=Bearer secret-token",
                    correlation_id="cid-diagnostics",
                ),
                RSSFeed(
                    url="https://feeds.example.com/private?token=secret",
                    title="private feed title",
                    fetch_error_count=2,
                    last_error="rss failed token=secret-rss",
                    updated_at=now,
                ),
                UserGitHubIntegration(
                    user_id=owner.telegram_user_id,
                    auth_method=GitHubAuthMethod.PAT,
                    encrypted_token=b"encrypted-secret",
                    status=GitHubIntegrationStatus.NEEDS_REAUTH,
                    updated_at=now,
                ),
                ImportJob(
                    user_id=owner.telegram_user_id,
                    source_format="html",
                    file_name="private-bookmarks.html",
                    status="failed",
                    errors_json=["import failed password=secret-import"],
                    updated_at=now,
                ),
            ]
        )
        summary_request = Request(type="url", status="completed", user_id=owner.telegram_user_id)
        session.add(summary_request)
        await session.flush()
        session.add(Summary(request_id=summary_request.id, lang="en", json_payload={}))

    response = client.get("/v1/admin/diagnostics", headers=_headers(6201))

    assert response.status_code == 200
    data = response.json()["data"]
    DiagnosticsResponse.model_validate(data)
    assert set(data) >= {
        "components",
        "scraper_providers",
        "llm_providers",
        "queue_backlog",
        "vector_indexing_lag",
        "latest_sync_failures",
        "storage_growth",
    }
    assert data["queue_backlog"]["by_status"]["failed"] == 1
    assert data["queue_backlog"]["runnable_count"] == 1
    assert data["vector_indexing_lag"]["missing_embeddings"] >= 1
    assert any(item["provider"] == "openrouter" for item in data["llm_providers"])
    assert any(item["provider"] == "direct_html" for item in data["scraper_providers"])
    assert any(item["source"] == "rss" for item in data["latest_sync_failures"])
    assert any(item["source"] == "github" for item in data["latest_sync_failures"])
    serialized = response.text
    assert "secret-token" not in serialized
    assert "secret-key" not in serialized
    assert "raw prompt secret" not in serialized
    assert "raw model output" not in serialized
    assert "raw-url-token" not in serialized


def test_admin_diagnostics_openapi_contract(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    operation = schema["paths"]["/v1/admin/diagnostics"]["get"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "DiagnosticsSuccessResponse"
    )
