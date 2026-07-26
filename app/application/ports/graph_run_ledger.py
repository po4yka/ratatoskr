"""Durable, content-free graph node chronology port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class GraphRunLedgerPort(Protocol):
    """Append structural graph-node lifecycle records without user content."""

    async def record_node(
        self, *, request_id: int, correlation_id: str, node: str, status: str
    ) -> None:
        """Record one node lifecycle transition on a durable run ledger."""
