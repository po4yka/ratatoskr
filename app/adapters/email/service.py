"""Email delivery service for verification, digests, and summary sends."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from app.adapters.email.protocol import EmailDeliveryProtocol, EmailMessage
from app.adapters.email.resend import ResendEmailProvider
from app.adapters.email.smtp import SMTPEmailProvider
from app.api.exceptions import FeatureDisabledError, ResourceNotFoundError, ValidationError
from app.core.logging_utils import get_logger
from app.infrastructure.persistence.email_delivery_store import EmailDeliveryStore
from app.observability.metrics_email import record_email_delivery

if TYPE_CHECKING:
    from app.config.email import EmailConfig

logger = get_logger(__name__)


class EmailDeliveryService:
    """Coordinates address verification and outbound email delivery."""

    def __init__(
        self,
        cfg: EmailConfig,
        *,
        store: EmailDeliveryStore | None = None,
        provider: EmailDeliveryProtocol | None = None,
    ) -> None:
        self._cfg = cfg
        self._store = store or EmailDeliveryStore()
        self._provider = provider or _build_provider(cfg)

    async def list_addresses(self, user_id: int) -> list[dict[str, Any]]:
        addresses = await self._store.async_list_addresses(user_id)
        return [
            {
                "id": address.id,
                "email": address.email,
                "is_verified": address.is_verified,
                "verified_at": address.verified_at,
                "created_at": address.created_at,
            }
            for address in addresses
        ]

    async def start_verification(self, *, user_id: int, email: str) -> dict[str, Any]:
        display_email, canonical_email = _canonicalize_email(email)
        if not display_email or not canonical_email:
            raise ValidationError("Email is required", details={"field": "email"})

        token = await self._store.async_start_verification(
            user_id=user_id,
            email=display_email,
            email_canonical=canonical_email,
        )
        link = self._verification_link(token.token)
        if self._cfg.provider == "none":
            record_email_delivery("disabled")
            return {
                "id": token.address.id,
                "email": token.address.email,
                "status": "pending",
                "email_sent": False,
                "verification_url": link,
            }

        subject = "Verify your Ratatoskr email address"
        text = f"Open this link to verify your email address:\n\n{link}\n\nIf you did not request this, ignore this email."
        await self._send_and_record(
            user_id=user_id,
            address_id=token.address.id,
            recipient=token.address.email,
            subject=subject,
            text=text,
            purpose="email_verification",
            correlation_id=None,
        )
        return {
            "id": token.address.id,
            "email": token.address.email,
            "status": "pending",
            "email_sent": True,
        }

    async def verify(self, token: str) -> dict[str, Any]:
        if not token.strip():
            raise ValidationError("Verification token is required", details={"field": "token"})
        address = await self._store.async_verify_token(token)
        if address is None:
            raise ValidationError("Verification token is invalid or expired")
        return {
            "id": address.id,
            "email": address.email,
            "is_verified": address.is_verified,
            "verified_at": address.verified_at,
        }

    async def send_digest(
        self,
        *,
        user_id: int,
        address_id: int | None,
        subject: str,
        text: str,
        correlation_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        address = await self._store.async_get_verified_address_for_user(
            user_id=user_id,
            address_id=address_id,
        )
        if address is None:
            raise ResourceNotFoundError("VerifiedEmailAddress", str(address_id or "default"))
        await self._send_and_record(
            user_id=user_id,
            address_id=address.id,
            recipient=address.email,
            subject=subject,
            text=text,
            html=_markdownish_to_html(text),
            purpose="digest",
            correlation_id=correlation_id,
            metadata=metadata,
        )

    async def send_custom_content(
        self,
        *,
        user_id: int,
        address_id: int | None,
        subject: str,
        content: str,
        purpose: str,
        correlation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        address = await self._store.async_get_verified_address_for_user(
            user_id=user_id,
            address_id=address_id,
        )
        if address is None:
            raise ResourceNotFoundError("VerifiedEmailAddress", str(address_id or "default"))
        delivery = await self._send_and_record(
            user_id=user_id,
            address_id=address.id,
            recipient=address.email,
            subject=subject,
            text=content,
            html=_markdownish_to_html(content),
            purpose=purpose,
            correlation_id=correlation_id,
            metadata=metadata,
        )
        return {"delivery_id": str(delivery.id), "status": delivery.status}

    async def send_magic_link(
        self,
        *,
        user_id: int,
        recipient: str,
        link: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        subject = "Your Ratatoskr sign-in link"
        text = f"Open this link to sign in to Ratatoskr:\n\n{link}\n\nThis link expires soon and can only be used once."
        if self._cfg.provider == "none":
            record_email_delivery("disabled")
            return {"email_sent": False, "magic_link": link}
        delivery = await self._send_and_record(
            user_id=user_id,
            address_id=None,
            recipient=recipient,
            subject=subject,
            text=text,
            html=_markdownish_to_html(text),
            purpose="magic_link",
            correlation_id=correlation_id,
        )
        return {"email_sent": True, "delivery_id": str(delivery.id)}

    async def _send_and_record(
        self,
        *,
        user_id: int,
        address_id: int | None,
        recipient: str,
        subject: str,
        text: str,
        purpose: str,
        correlation_id: str | None,
        html: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        if self._cfg.provider == "none":
            record_email_delivery("disabled")
            raise FeatureDisabledError("email")

        result = await self._provider.send(
            EmailMessage(to=recipient, subject=subject, text=text, html=html)
        )
        delivery = await self._store.async_record_delivery(
            user_id=user_id,
            email_address_id=address_id,
            provider=result.provider,
            recipient=recipient,
            subject=subject,
            status=result.status,
            purpose=purpose,
            correlation_id=correlation_id,
            provider_message_id=result.provider_message_id,
            error=result.error,
            metadata=metadata,
        )
        record_email_delivery(result.status)
        if result.status != "sent":
            logger.warning(
                "email_delivery_failed",
                extra={
                    "uid": user_id,
                    "provider": result.provider,
                    "purpose": purpose,
                    "cid": correlation_id,
                    "error": result.error,
                },
            )
            raise RuntimeError(result.error or "Email delivery failed")
        return delivery

    def _verification_link(self, token: str) -> str:
        base = self._cfg.verification_base_url
        if not base:
            return token
        separator = "&" if "?" in base else "?"
        return f"{base}{separator}token={token}"


def _build_provider(cfg: EmailConfig) -> EmailDeliveryProtocol:
    if cfg.provider == "smtp":
        return SMTPEmailProvider(cfg)
    return ResendEmailProvider(cfg)


def _markdownish_to_html(text: str) -> str:
    escaped = html.escape(text)
    return (
        '<pre style="white-space:pre-wrap;font-family:system-ui,sans-serif">' + escaped + "</pre>"
    )


def _canonicalize_email(email: str) -> tuple[str | None, str | None]:
    cleaned = email.strip()
    if not cleaned:
        return None, None
    if "@" not in cleaned:
        raise ValidationError("Email must contain '@'", details={"field": "email"})
    if len(cleaned) > 256:
        raise ValidationError("Email must be at most 256 characters", details={"field": "email"})
    return cleaned, cleaned.casefold()
