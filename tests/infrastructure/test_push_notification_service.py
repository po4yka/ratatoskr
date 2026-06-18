from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

import app.infrastructure.push.service as push_module
from app.config.push import PushNotificationConfig
from app.infrastructure.persistence.repositories.device_repository import DeviceRepositoryAdapter
from app.infrastructure.push.service import (
    PushNotificationService,
    create_push_notification_service,
)


class _Messaging:
    def __init__(self, *, send_raises: Exception | None = None) -> None:
        self.send_raises = send_raises
        self.sent: list[Any] = []

        class Notification:
            def __init__(self, *, title: str, body: str) -> None:
                self.title = title
                self.body = body

        class Aps:
            def __init__(self, *, sound: str, badge: int) -> None:
                self.sound = sound
                self.badge = badge

        class APNSPayload:
            def __init__(self, *, aps: Any) -> None:
                self.aps = aps

        class APNSConfig:
            def __init__(self, *, payload: Any) -> None:
                self.payload = payload

        class AndroidNotification:
            def __init__(self, *, sound: str) -> None:
                self.sound = sound

        class AndroidConfig:
            def __init__(self, *, notification: Any) -> None:
                self.notification = notification

        class Message:
            def __init__(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

        self.Notification = Notification
        self.Aps = Aps
        self.APNSPayload = APNSPayload
        self.APNSConfig = APNSConfig
        self.AndroidNotification = AndroidNotification
        self.AndroidConfig = AndroidConfig
        self.Message = Message

    def send(self, message: Any) -> str:
        if self.send_raises is not None:
            raise self.send_raises
        self.sent.append(message)
        return "firebase-response"


class _Credentials:
    def __init__(self) -> None:
        self.paths: list[str] = []

    def Certificate(self, path: str) -> dict[str, str]:  # noqa: N802 - firebase API shape
        self.paths.append(path)
        return {"path": path}


class _FirebaseAdmin:
    def __init__(self) -> None:
        self.apps: list[Any] = []

    def initialize_app(self, credential: Any) -> object:
        self.apps.append(credential)
        return object()


def _config(
    *, enabled: bool = True, path: str | None = "/tmp/firebase.json"
) -> PushNotificationConfig:
    return cast(
        "PushNotificationConfig",
        SimpleNamespace(enabled=enabled, firebase_credentials_path=path),
    )


def _repo(**methods: Any) -> DeviceRepositoryAdapter:
    return cast("DeviceRepositoryAdapter", SimpleNamespace(**methods))


@pytest.fixture
def firebase(monkeypatch: pytest.MonkeyPatch) -> tuple[_Messaging, _Credentials, _FirebaseAdmin]:
    messaging = _Messaging()
    credentials = _Credentials()
    firebase_admin = _FirebaseAdmin()
    monkeypatch.setattr(push_module, "_FIREBASE_AVAILABLE", True)
    monkeypatch.setattr(push_module, "messaging", messaging)
    monkeypatch.setattr(push_module, "credentials", credentials)
    monkeypatch.setattr(push_module, "firebase_admin", firebase_admin)
    return messaging, credentials, firebase_admin


def test_push_initialize_is_idempotent_and_uses_configured_credentials(
    firebase: tuple[_Messaging, _Credentials, _FirebaseAdmin],
) -> None:
    _messaging, credentials, firebase_admin = firebase
    service = PushNotificationService(_config(), _repo())

    service.initialize()
    service.initialize()

    assert service._initialized is True
    assert credentials.paths == ["/tmp/firebase.json"]
    assert len(firebase_admin.apps) == 1


def test_push_initialize_noops_when_disabled_or_misconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(push_module, "_FIREBASE_AVAILABLE", False)
    disabled = PushNotificationService(_config(enabled=False), _repo())
    missing_sdk = PushNotificationService(_config(), _repo())

    disabled.initialize()
    missing_sdk.initialize()

    assert disabled._initialized is False
    assert missing_sdk._initialized is False


@pytest.mark.asyncio
async def test_push_send_to_user_skips_empty_tokens_and_uses_device_platform(
    firebase: tuple[_Messaging, _Credentials, _FirebaseAdmin],
) -> None:
    messaging, _credentials, _firebase_admin = firebase
    async_list_user_devices = AsyncMock(
        return_value=[
            {"token": "", "platform": "android"},
            {"token": "ios-token", "platform": "ios"},
            {"token": "android-token"},
        ]
    )
    repo = _repo(async_list_user_devices=async_list_user_devices)
    service = PushNotificationService(_config(), repo)
    service.initialize()

    await service.send_to_user(5, "Title", "Body", {"summary_id": "1"})

    assert async_list_user_devices.await_args.kwargs == {"active_only": True}
    assert [message.kwargs["token"] for message in messaging.sent] == [
        "ios-token",
        "android-token",
    ]
    assert "apns" in messaging.sent[0].kwargs
    assert "android" in messaging.sent[1].kwargs


@pytest.mark.asyncio
async def test_push_send_to_device_deactivates_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
    firebase: tuple[_Messaging, _Credentials, _FirebaseAdmin],
) -> None:
    messaging, _credentials, _firebase_admin = firebase

    class InvalidArgumentError(Exception):
        pass

    messaging.send_raises = InvalidArgumentError("invalid token")
    monkeypatch.setattr(
        push_module.firebase_admin,
        "exceptions",
        SimpleNamespace(NotFoundError=LookupError, InvalidArgumentError=InvalidArgumentError),
        raising=False,
    )
    async_deactivate_device = AsyncMock()
    repo = _repo(async_deactivate_device=async_deactivate_device)
    service = PushNotificationService(_config(), repo)
    service.initialize()

    await service.send_to_device(
        token="dead-token",
        platform="android",
        title="Title",
        body="Body",
        data=None,
    )
    await asyncio.sleep(0)

    async_deactivate_device.assert_awaited_once_with("dead-token")


def test_create_push_notification_service_initializes(firebase: tuple[Any, Any, Any]) -> None:
    service = create_push_notification_service(_config(), _repo())

    assert service._initialized is True
