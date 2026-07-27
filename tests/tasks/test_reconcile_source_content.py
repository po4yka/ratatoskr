from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


def _stub_taskiq(monkeypatch: pytest.MonkeyPatch) -> None:
    for module_name in (
        "taskiq",
        "taskiq.message",
        "taskiq_redis",
    ):
        if module_name not in sys.modules:
            monkeypatch.setitem(sys.modules, module_name, types.ModuleType(module_name))
    taskiq = sys.modules["taskiq"]
    taskiq.AsyncBroker = object
    taskiq.TaskiqDepends = lambda function, **_kwargs: None
    taskiq.TaskiqMiddleware = object
    taskiq.InMemoryBroker = MagicMock
    sys.modules["taskiq.message"].TaskiqMessage = object
    sys.modules["taskiq_redis"].RedisStreamBroker = MagicMock
    sys.modules["taskiq_redis"].RedisAsyncResultBackend = MagicMock


def _load_task(monkeypatch: pytest.MonkeyPatch):
    _stub_taskiq(monkeypatch)
    monkeypatch.setenv("TASKIQ_BROKER", "memory")
    for module_name in list(sys.modules):
        if module_name.startswith("app.tasks"):
            sys.modules.pop(module_name, None)
    from app.tasks import reconcile_source_content

    return reconcile_source_content


def _cfg(
    *,
    enabled: bool = True,
    privacy: bool = False,
    batch_size: int = 25,
    network_limit: int = 1,
) -> SimpleNamespace:
    return SimpleNamespace(
        retention=SimpleNamespace(
            source_content_reconcile_enabled=enabled,
            privacy_no_retention_mode=privacy,
            source_content_reconcile_batch_size=batch_size,
            source_content_reconcile_network_limit=network_limit,
        )
    )


@pytest.mark.asyncio
async def test_reconcile_prefers_local_content_and_bounds_network_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_task(monkeypatch)
    rows = [
        {
            "summary_id": 1,
            "request_id": 11,
            "user_id": 7,
            "has_local_source": True,
            "missing_total": 3,
        },
        {
            "summary_id": 2,
            "request_id": 22,
            "user_id": 7,
            "has_local_source": False,
            "missing_total": 3,
        },
        {
            "summary_id": 3,
            "request_id": 33,
            "user_id": 7,
            "has_local_source": False,
            "missing_total": 3,
        },
    ]
    monkeypatch.setattr(module, "_fetch_missing_source_rows", AsyncMock(return_value=rows))

    async def backfill(**kwargs):
        if kwargs["summary_id"] == 1:
            assert kwargs["allow_reextract"] is False
            return SimpleNamespace(reextracted=False)
        if kwargs["summary_id"] == 2:
            assert kwargs["allow_reextract"] is True
            return SimpleNamespace(reextracted=True)
        assert kwargs["allow_reextract"] is False
        raise module.SourceContentBackfillUnavailableError("budget")

    service = SimpleNamespace(backfill=AsyncMock(side_effect=backfill))
    summary = await module._reconcile_body(
        _cfg(),
        MagicMock(),
        service=service,
        correlation_id="reconcile-test",
    )

    assert summary == module.SourceContentReconcileSummary(
        scanned=3,
        local_repaired=1,
        reextracted=1,
        skipped=1,
        failed=0,
        missing_remaining=1,
    )
    assert service.backfill.await_count == 3


@pytest.mark.asyncio
async def test_reconcile_is_disabled_in_privacy_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_task(monkeypatch)
    fetch = AsyncMock()
    monkeypatch.setattr(module, "_fetch_missing_source_rows", fetch)

    summary = await module._reconcile_body(_cfg(privacy=True), MagicMock())

    assert summary == module.SourceContentReconcileSummary()
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_counts_row_failure_without_stopping_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_task(monkeypatch)
    monkeypatch.setattr(
        module,
        "_fetch_missing_source_rows",
        AsyncMock(
            return_value=[
                {
                    "summary_id": 1,
                    "request_id": 11,
                    "user_id": 7,
                    "has_local_source": True,
                    "missing_total": 1,
                }
            ]
        ),
    )
    service = SimpleNamespace(backfill=AsyncMock(side_effect=RuntimeError("db write failed")))

    summary = await module._reconcile_body(_cfg(), MagicMock(), service=service)

    assert summary.failed == 1
    assert summary.missing_remaining == 1
