"""Email delivery protocol and shared DTOs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class EmailMessage:
    """Outbound email message payload."""

    to: str
    subject: str
    text: str
    html: str | None = None


@dataclass(frozen=True)
class EmailDeliveryResult:
    """Provider delivery result."""

    provider: str
    status: str
    provider_message_id: str | None = None
    error: str | None = None


class EmailDeliveryProtocol(Protocol):
    """Protocol implemented by concrete email providers."""

    provider_name: str

    async def send(self, message: EmailMessage) -> EmailDeliveryResult:
        """Send an outbound email message."""
