from __future__ import annotations

from app.api.routers.auth.tokens import create_access_token
from app.config import Config
from app.db.models import Request, User


def test_status_endpoint_returns_flat_status_payload(client):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424242
    user = User.create(telegram_user_id=user_id, username="status_shape_user")
    request = Request.create(
        user_id=user.telegram_user_id,
        type="url",
        status="pending",
        correlation_id="cid-shape-1",
        input_url="https://example.com/status-shape",
        normalized_url="https://example.com/status-shape",
        dedupe_hash="shape-hash-1",
        lang_detected="en",
    )

    token = create_access_token(user.telegram_user_id, client_id="test")
    response = client.get(
        f"/v1/requests/{request.id}/status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True

    data = payload["data"]
    assert data["requestId"] == request.id
    assert data["status"] == "pending"
    assert data["legacyStatus"] == "pending"
    assert data["stage"] == "queued"
    assert data["canRetry"] is False
    assert not isinstance(data["status"], dict)


def test_status_endpoint_uses_shared_public_lifecycle_mapping(client):
    allowed_ids = Config.get_allowed_user_ids()
    user_id = int(allowed_ids[0]) if allowed_ids else 424243
    user = User.create(telegram_user_id=user_id, username="status_mapping_user")
    request = Request.create(
        user_id=user.telegram_user_id,
        type="url",
        status="processing",
        correlation_id="cid-shape-2",
        input_url="https://example.com/status-mapping",
        normalized_url="https://example.com/status-mapping",
        dedupe_hash="shape-hash-2",
        lang_detected="en",
    )

    token = create_access_token(user.telegram_user_id, client_id="test")
    response = client.get(
        f"/v1/requests/{request.id}/status",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "running"
    assert data["legacyStatus"] == "processing"
    assert data["stage"] == "extracting"
