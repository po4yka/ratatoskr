"""Owner-only, privacy-safe graph run ledger API coverage."""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.api.models.responses.graph_run_ledger import (
    GraphRunEvaluationListResponse,
    GraphRunLedgerResponse,
)
from app.api.routers.auth.tokens import create_access_token
from app.core.time_utils import UTC
from app.db.models import LLMCall, ProgressEvent, Request, Summary, SummaryFeedback, User


def _headers(user_id: int) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id, client_id='test')}"}


@pytest.mark.asyncio
async def test_graph_run_ledger_is_owner_only_and_excludes_sensitive_values(
    client: TestClient, db
) -> None:
    now = dt.datetime.now(UTC)
    async with db.transaction() as session:
        owner = User(telegram_user_id=9101, username="ledger-owner", is_owner=True)
        regular_user = User(telegram_user_id=9102, username="ledger-user", is_owner=False)
        session.add_all([owner, regular_user])
        request = Request(
            type="url",
            status="completed",
            user_id=regular_user.telegram_user_id,
            input_url="https://example.com/private?token=raw-url-token",
            content_text="private article body",
            processing_time_ms=4321,
            created_at=now,
        )
        session.add(request)
        await session.flush()
        summary = Summary(request_id=request.id, lang="en", json_payload={})
        session.add(summary)
        await session.flush()
        session.add_all(
            [
                ProgressEvent(
                    request_id=request.id,
                    event_id="ledger-event-1",
                    sequence=1,
                    kind="stage",
                    stage="build_prompt",
                    status="running",
                    message="raw article body and token=secret",
                    payload={"prompt": "private raw prompt"},
                    created_at=now,
                ),
                LLMCall(
                    request_id=request.id,
                    attempt_index=1,
                    attempt_trigger="initial",
                    provider="openrouter",
                    model="test/model",
                    status="error",
                    latency_ms=100,
                    total_latency_ms=150,
                    tokens_prompt=11,
                    tokens_completion=22,
                    cost_usd=0.012345,
                    request_messages_json=[{"content": "private raw prompt"}],
                    response_text="private raw completion",
                    error_text="provider secret=raw-error",
                    fallback_model_used="fallback/model",
                    retry_exhausted=True,
                ),
                LLMCall(
                    request_id=request.id,
                    attempt_index=2,
                    attempt_trigger="repair_loop",
                    provider="openrouter",
                    model="test/model",
                    status="success",
                    latency_ms=200,
                    tokens_prompt=33,
                    tokens_completion=44,
                    cost_usd=0.02,
                ),
                SummaryFeedback(
                    user_id=regular_user.telegram_user_id,
                    summary_id=summary.id,
                    rating=2,
                    issues='["wrong_scope", "hallucination"]',
                    comment="private free-form feedback",
                    updated_at=now,
                ),
            ]
        )

    forbidden = client.get(f"/v1/admin/graph-runs/{request.id}", headers=_headers(9102))
    assert forbidden.status_code == 403

    response = client.get(f"/v1/admin/graph-runs/{request.id}", headers=_headers(9101))
    assert response.status_code == 200
    data = response.json()["data"]
    GraphRunLedgerResponse.model_validate(data)
    assert data["requestId"] == request.id
    assert data["chronology"] == [
        {
            "sequence": 1,
            "kind": "stage",
            "stage": "build_prompt",
            "status": "running",
            "occurredAt": now.isoformat().replace("+00:00", "Z"),
        }
    ]
    assert data["metrics"] == {
        "nodeCount": 1,
        "attemptCount": 2,
        "repairCount": 1,
        "fallbackCount": 1,
        "graphLatencyMs": 4321,
        "llmLatencyMs": 300,
        "promptTokens": 44,
        "completionTokens": 66,
        "totalCostUsd": 0.032345,
    }
    assert data["attempts"][0]["errorPresent"] is True
    assert data["attempts"][0]["fallbackModel"] == "fallback/model"
    assert data["feedback"] == {
        "feedbackCount": 1,
        "ratingAverage": 2.0,
        "issueCount": 2,
        "latestFeedbackAt": now.isoformat().replace("+00:00", "Z"),
    }
    rendered = response.text
    for secret in (
        "raw-url-token",
        "private article body",
        "private raw prompt",
        "private raw completion",
        "raw-error",
        "private free-form feedback",
    ):
        assert secret not in rendered


@pytest.mark.asyncio
async def test_graph_run_evaluations_returns_bounded_feedback_join(client: TestClient, db) -> None:
    async with db.transaction() as session:
        owner = User(telegram_user_id=9201, username="evaluation-owner", is_owner=True)
        session.add(owner)
        request = Request(type="url", status="completed", user_id=owner.telegram_user_id)
        session.add(request)
        await session.flush()
        summary = Summary(request_id=request.id, lang="en", json_payload={})
        session.add(summary)
        await session.flush()
        session.add(
            SummaryFeedback(
                user_id=owner.telegram_user_id,
                summary_id=summary.id,
                rating=5,
                issues='["clear"]',
                comment="excluded from offline evaluation projection",
            )
        )

    response = client.get("/v1/admin/graph-runs?limit=1", headers=_headers(9201))
    assert response.status_code == 200
    data = response.json()["data"]
    GraphRunEvaluationListResponse.model_validate(data)
    assert data["limit"] == 1
    assert len(data["items"]) == 1
    assert data["items"][0]["requestId"] == request.id
    assert data["items"][0]["feedback"]["ratingAverage"] == 5.0
    assert "excluded from offline evaluation projection" not in response.text
