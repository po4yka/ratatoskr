"""SMTP email delivery adapter."""

from __future__ import annotations

import asyncio
import smtplib
from email.message import EmailMessage as SMTPEmailMessage
from typing import TYPE_CHECKING

from app.adapters.email.protocol import EmailDeliveryResult, EmailMessage

if TYPE_CHECKING:
    from app.config.email import EmailConfig


class SMTPEmailProvider:
    """Send email through a configured SMTP server."""

    provider_name = "smtp"

    def __init__(self, cfg: EmailConfig) -> None:
        self._cfg = cfg

    async def send(self, message: EmailMessage) -> EmailDeliveryResult:
        return await asyncio.to_thread(self._send_sync, message)

    def _send_sync(self, message: EmailMessage) -> EmailDeliveryResult:
        if not self._cfg.smtp_host:
            return EmailDeliveryResult(
                provider=self.provider_name,
                status="failed",
                error="SMTP_HOST is not configured",
            )
        if not self._cfg.from_address:
            return EmailDeliveryResult(
                provider=self.provider_name,
                status="failed",
                error="EMAIL_FROM_ADDRESS is not configured",
            )

        email = SMTPEmailMessage()
        email["From"] = f"{self._cfg.from_name} <{self._cfg.from_address}>"
        email["To"] = message.to
        email["Subject"] = message.subject
        email.set_content(message.text)
        if message.html:
            email.add_alternative(message.html, subtype="html")

        try:
            with smtplib.SMTP(
                self._cfg.smtp_host,
                self._cfg.smtp_port,
                timeout=self._cfg.timeout_seconds,
            ) as server:
                if self._cfg.smtp_use_tls:
                    server.starttls()
                if self._cfg.smtp_username:
                    server.login(self._cfg.smtp_username, self._cfg.smtp_password or "")
                server.send_message(email)
        except (OSError, smtplib.SMTPException) as exc:
            return EmailDeliveryResult(
                provider=self.provider_name,
                status="failed",
                error=str(exc)[:500],
            )

        return EmailDeliveryResult(provider=self.provider_name, status="sent")
