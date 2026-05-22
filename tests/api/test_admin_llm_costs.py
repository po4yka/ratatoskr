from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.routers.auth.tokens import create_access_token
from app.db.models import LLMCall, Request, User


def _headers(user_id: int) -> dict[str, str]:
    token = create_access_token(user_id, client_id="test")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_llm_costs_requires_owner(client: TestClient, db) -> None:
    async with db.transaction() as session:
        owner = User(telegram_user_id=1001, username="owner", is_owner=True)
        non_owner = User(telegram_user_id=1002, username="non-owner", is_owner=False)
        session.add_all([owner, non_owner])

    forbidden = client.get("/v1/admin/llm-costs", headers=_headers(1002))
    assert forbidden.status_code == 403

    ok = client.get("/v1/admin/llm-costs", headers=_headers(1001))
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_admin_llm_costs_returns_redacted_aggregates(client: TestClient, db) -> None:
    async with db.transaction() as session:
        owner = User(telegram_user_id=2001, username="owner-costs", is_owner=True)
        request = Request(type="url", status="completed", user_id=owner.telegram_user_id)
        session.add(request)
        await session.flush()
        session.add(
            LLMCall(
                request_id=request.id,
                provider="openrouter",
                model="test/model",
                status="ok",
                request_messages_json=[{"role": "user", "content": "secret prompt"}],
                response_text="secret response",
                tokens_prompt=10,
                tokens_completion=5,
                cost_usd=0.0123,
                latency_ms=250,
            )
        )

    response = client.get("/v1/admin/llm-costs?days=30", headers=_headers(2001))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["totals"]["calls"] == 1
    assert data["totals"]["prompt_tokens"] == 10
    assert data["totals"]["completion_tokens"] == 5
    assert data["totals"]["cost_usd"] == 0.0123
    assert data["by_provider_model"] == [
        {
            "provider": "openrouter",
            "model": "test/model",
            "status": "ok",
            "calls": 1,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cost_usd": 0.0123,
            "avg_latency_ms": 250.0,
        }
    ]
    serialized = response.text
    assert "secret prompt" not in serialized
    assert "secret response" not in serialized
