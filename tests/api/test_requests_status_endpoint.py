from __future__ import annotations

import pytest

from app.api.routers.auth.tokens import create_access_token
from app.config import Config
from app.db.models import Request, User


@pytest.mark.asyncio
async def test_status_endpoint_returns_flat_status_payload(client, db):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    async with db.transaction() as session:
        session.add(User(telegram_user_id=user_id, username="status_shape_user"))
        request = Request(
            user_id=user_id,
            type="url",
            status="pending",
            correlation_id="cid-shape-1",
            input_url="https://example.com/status-shape",
            normalized_url="https://example.com/status-shape",
            dedupe_hash="shape-hash-1",
            lang_detected="en",
        )
        session.add(request)
        await session.flush()
        request_id = request.id

    token = create_access_token(user_id, client_id="test")
    response = client.get(
        f"/v1/requests/{request_id}/status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    data = payload["data"]
    assert data["requestId"] == request_id
    assert data["status"] == "pending"
    assert data["legacyStatus"] == "pending"
    assert data["stage"] == "queued"
    assert data["canRetry"] is False
    assert not isinstance(data["status"], dict)


@pytest.mark.asyncio
async def test_status_endpoint_uses_shared_public_lifecycle_mapping(client, db):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424243
    async with db.transaction() as session:
        session.add(User(telegram_user_id=user_id, username="status_mapping_user"))
        request = Request(
            user_id=user_id,
            type="url",
            status="processing",
            correlation_id="cid-shape-2",
            input_url="https://example.com/status-mapping",
            normalized_url="https://example.com/status-mapping",
            dedupe_hash="shape-hash-2",
            lang_detected="en",
        )
        session.add(request)
        await session.flush()
        request_id = request.id

    token = create_access_token(user_id, client_id="test")
    response = client.get(
        f"/v1/requests/{request_id}/status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "running"
    assert data["legacyStatus"] == "processing"
    assert data["stage"] == "extracting"
