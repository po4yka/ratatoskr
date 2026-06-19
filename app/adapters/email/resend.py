"""Resend email delivery adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from app.adapters.email.protocol import EmailDeliveryResult, EmailMessage

if TYPE_CHECKING:
    from app.config.email import EmailConfig


class ResendEmailProvider:
    """Send email via the Resend HTTP API."""

    provider_name = "resend"

    def __init__(self, cfg: EmailConfig) -> None:
        self._cfg = cfg

    async def send(self, message: EmailMessage) -> EmailDeliveryResult:
        if not self._cfg.resend_api_key:
            return EmailDeliveryResult(
                provider=self.provider_name,
                status="failed",
                error="RESEND_API_KEY is not configured",
            )
        if not self._cfg.from_address:
            return EmailDeliveryResult(
                provider=self.provider_name,
                status="failed",
                error="EMAIL_FROM_ADDRESS is not configured",
            )
        payload: dict[str, object] = {
            "from": f"{self._cfg.from_name} <{self._cfg.from_address}>",
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
        }
        if message.html:
            payload["html"] = message.html

        try:
            async with httpx.AsyncClient(timeout=self._cfg.timeout_seconds) as client:
                response = await client.post(
                    self._cfg.resend_api_url,
                    headers={"Authorization": f"Bearer {self._cfg.resend_api_key}"},
                    json=payload,
                )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return EmailDeliveryResult(
                provider=self.provider_name,
                status="failed",
                error=str(exc)[:500],
            )

        data = response.json()
        message_id = data.get("id") if isinstance(data, dict) else None
        return EmailDeliveryResult(
            provider=self.provider_name,
            status="sent",
            provider_message_id=str(message_id) if message_id else None,
        )
