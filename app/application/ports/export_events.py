"""Ports for outbound export event publication."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SummaryExportEventPublisherPort(Protocol):
    async def publish_summary_created(self, summary_id: int) -> None:
        """Publish a newly persisted summary to outbound export integrations."""


__all__ = ["SummaryExportEventPublisherPort"]
