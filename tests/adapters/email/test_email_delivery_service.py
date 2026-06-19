from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.email.protocol import EmailDeliveryResult, EmailMessage
from app.adapters.email.service import EmailDeliveryService
from app.api.exceptions import FeatureDisabledError
from app.config.email import EmailConfig
from app.core.time_utils import UTC
from app.infrastructure.persistence.email_delivery_store import EmailVerificationToken


class FakeProvider:
    provider_name = "fake"

    def __init__(self, result: EmailDeliveryResult | None = None) -> None:
        self.messages: list[EmailMessage] = []
        self._result = result or EmailDeliveryResult(
            provider="fake",
            status="sent",
            provider_message_id="msg_1",
        )

    async def send(self, message: EmailMessage) -> EmailDeliveryResult:
        self.messages.append(message)
        return self._result


class FakeStore:
    def __init__(self) -> None:
        self.address = SimpleNamespace(
            id=7,
            user_id=42,
            email="user@example.com",
            email_canonical="user@example.com",
            is_verified=True,
            verified_at=datetime(2026, 6, 19, tzinfo=UTC),
            created_at=datetime(2026, 6, 19, tzinfo=UTC),
        )
        self.recorded: list[dict[str, Any]] = []

    async def async_list_addresses(self, user_id: int) -> list[Any]:
        return [self.address] if user_id == self.address.user_id else []

    async def async_start_verification(
        self,
        *,
        user_id: int,
        email: str,
        email_canonical: str,
        ttl: Any = None,
    ) -> EmailVerificationToken:
        self.address.user_id = user_id
        self.address.email = email
        self.address.email_canonical = email_canonical
        return EmailVerificationToken(address=self.address, token="verification-token")  # type: ignore[arg-type]

    async def async_verify_token(self, token: str) -> Any:
        return self.address if token == "verification-token" else None

    async def async_get_verified_address_for_user(
        self,
        *,
        user_id: int,
        address_id: int | None,
    ) -> Any:
        if user_id != self.address.user_id:
            return None
        if address_id is not None and address_id != self.address.id:
            return None
        return self.address

    async def async_record_delivery(self, **kwargs: Any) -> Any:
        self.recorded.append(kwargs)
        return SimpleNamespace(id="delivery-1", status=kwargs["status"])


@pytest.mark.asyncio
async def test_start_verification_returns_token_link_when_email_disabled() -> None:
    store = FakeStore()
    service = EmailDeliveryService(
        EmailConfig(provider="none", verification_base_url="https://app.example/verify"),
        store=store,  # type: ignore[arg-type]
        provider=FakeProvider(),
    )

    result = await service.start_verification(user_id=42, email="User@Example.com")

    assert result["email_sent"] is False
    assert result["verification_url"] == "https://app.example/verify?token=verification-token"
    assert store.address.email_canonical == "user@example.com"


@pytest.mark.asyncio
async def test_send_custom_content_records_successful_delivery() -> None:
    store = FakeStore()
    provider = FakeProvider()
    service = EmailDeliveryService(
        EmailConfig(provider="resend", from_address="noreply@example.com", resend_api_key="key"),
        store=store,  # type: ignore[arg-type]
        provider=provider,
    )

    result = await service.send_custom_content(
        user_id=42,
        address_id=7,
        subject="Digest",
        content="Hello",
        purpose="custom_digest",
    )

    assert result == {"delivery_id": "delivery-1", "status": "sent"}
    assert provider.messages[0].to == "user@example.com"
    assert store.recorded[0]["status"] == "sent"
    assert store.recorded[0]["provider_message_id"] == "msg_1"


@pytest.mark.asyncio
async def test_send_custom_content_fails_when_email_disabled() -> None:
    service = EmailDeliveryService(
        EmailConfig(provider="none"),
        store=FakeStore(),  # type: ignore[arg-type]
        provider=FakeProvider(),
    )

    with pytest.raises(FeatureDisabledError):
        await service.send_custom_content(
            user_id=42,
            address_id=7,
            subject="Digest",
            content="Hello",
            purpose="custom_digest",
        )
