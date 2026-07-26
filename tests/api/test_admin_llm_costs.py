from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api.routers.auth.tokens import create_access_token
from app.config import Config
from app.db.models import LLMCall, Request, User


def _headers(user_id: int) -> dict[str, str]:
    token = create_access_token(user_id, client_id="test")
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_admin_llm_costs_requires_owner(client: TestClient, db) -> None:
    user_id = int(Config.get_allowed_user_ids()[0])
    async with db.transaction() as session:
        session.add(User(telegram_user_id=user_id, username="non-owner", is_owner=False))

    forbidden = client.get("/v1/admin/llm-costs", headers=_headers(user_id))
    assert forbidden.status_code == 403

    async with db.transaction() as session:
        await session.execute(
            update(User).where(User.telegram_user_id == user_id).values(is_owner=True)
        )

    ok = client.get("/v1/admin/llm-costs", headers=_headers(user_id))
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_admin_llm_costs_returns_redacted_aggregates(client: TestClient, db) -> None:
    user_id = int(Config.get_allowed_user_ids()[0])
    async with db.transaction() as session:
        owner = User(telegram_user_id=user_id, username="owner-costs", is_owner=True)
        request = Request(type="url", status="completed", user_id=owner.telegram_user_id)
        session.add_all([owner, request])
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

    response = client.get("/v1/admin/llm-costs?days=30", headers=_headers(user_id))

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
