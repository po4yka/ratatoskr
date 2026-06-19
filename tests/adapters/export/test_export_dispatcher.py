"""Tests for export dispatch delivery logging."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.adapters.export.base import ExportPayload, ExportResult
from app.adapters.export.dispatcher import _adapter_for_integration, _log_delivery
from app.db.models import ExportDeliveryLog, UserExportIntegration


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, item: Any) -> None:
        self.added.append(item)


class _FakeDatabase:
    def __init__(self) -> None:
        self.session = _FakeSession()

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield self.session


def _payload() -> ExportPayload:
    return ExportPayload(
        summary_id=42,
        request_id=7,
        url="https://example.com",
        title="Title",
        tldr="TLDR",
        summary_250="Summary",
    )


@pytest.mark.asyncio
async def test_log_delivery_persists_failure_shape() -> None:
    db = _FakeDatabase()
    integration = UserExportIntegration(id=9, user_id=1, provider="readwise", enabled=True)

    await _log_delivery(
        db,
        integration=integration,
        payload=_payload(),
        result=ExportResult(success=False, response_status=500, error="boom"),
        duration_ms=123,
    )

    assert len(db.session.added) == 1
    row = db.session.added[0]
    assert isinstance(row, ExportDeliveryLog)
    assert row.integration_id == 9
    assert row.provider == "readwise"
    assert row.event_type == "summary.created"
    assert row.summary_id == 42
    assert row.success is False
    assert row.response_status == 500
    assert row.error == "boom"
    assert row.duration_ms == 123


def test_adapter_for_integration_rejects_unknown_provider() -> None:
    integration = UserExportIntegration(id=1, user_id=1, provider="unknown", enabled=True)

    with pytest.raises(ValueError, match="Unsupported export provider"):
        _adapter_for_integration(integration)
