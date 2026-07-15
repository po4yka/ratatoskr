import pytest
from sqlalchemy import select

from app.api.routers.auth.tokens import create_access_token
from app.db.models import UserDevice


@pytest.mark.asyncio
async def test_register_new_device(client, db, user_factory):
    user = await user_factory(username="notif_user", telegram_user_id=123456789)
    token = create_access_token(
        user_id=user.telegram_user_id, username=user.username, client_id="test_client"
    )
    headers = {"Authorization": f"Bearer {token}"}

    payload = {"token": "fcm_token_123", "platform": "android", "device_id": "device_123"}

    response = client.post("/v1/notifications/device", json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ok"

    async with db.session() as session:
        device = await session.scalar(select(UserDevice).where(UserDevice.token == "fcm_token_123"))
    assert device is not None
    assert device.user_id == user.telegram_user_id
    assert device.platform == "android"
    assert device.is_active is True


@pytest.mark.asyncio
async def test_update_existing_device(client, db, user_factory):
    user = await user_factory(username="notif_user_2", telegram_user_id=987654321)
    token = create_access_token(
        user_id=user.telegram_user_id, username=user.username, client_id="test_client"
    )
    headers = {"Authorization": f"Bearer {token}"}

    # Register first
    payload = {"token": "fcm_token_456", "platform": "ios", "device_id": "device_456"}
    client.post("/v1/notifications/device", json=payload, headers=headers)

    # Update same token with new details
    payload_update = {
        "token": "fcm_token_456",
        "platform": "ios",
        "device_id": "device_456_updated",
    }
    response = client.post("/v1/notifications/device", json=payload_update, headers=headers)
    assert response.status_code == 200

    async with db.session() as session:
        device = await session.scalar(select(UserDevice).where(UserDevice.token == "fcm_token_456"))
    assert device is not None
    assert device.device_id == "device_456_updated"


@pytest.mark.asyncio
async def test_register_device_invalid_payload(client, user_factory):
    user = await user_factory(username="notif_user_3", telegram_user_id=123456789)
    token = create_access_token(
        user_id=user.telegram_user_id, username=user.username, client_id="test_client"
    )
    headers = {"Authorization": f"Bearer {token}"}

    # Missing platform
    payload = {"token": "fcm_token_789"}
    response = client.post("/v1/notifications/device", json=payload, headers=headers)
    assert response.status_code == 422
