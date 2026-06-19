"""Tests for operational SSE stream primitives."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.adapters.content.streaming.operation_streams import (
    OperationStreamHub,
    OperationStreamEvent,
    digest_run_topic,
    github_sync_topic,
    publish_operation_event,
    vector_reconcile_topic,
)
from app.api.routers.operation_streams import _to_sse_event


@pytest.mark.asyncio
async def test_operation_stream_hub_replays_at_most_one_event_and_terminates() -> None:
    hub = OperationStreamHub()
    topic = "test:run"

    hub.publish(
        topic,
        OperationStreamEvent.now(kind="phase", payload={"phase": "one"}, correlation_id="cid"),
    )
    hub.publish(
        topic,
        OperationStreamEvent.now(kind="phase", payload={"phase": "two"}, correlation_id="cid"),
    )
    hub.publish(
        topic, OperationStreamEvent.now(kind="done", payload={"ok": True}, correlation_id="cid")
    )

    received = []
    async for event in hub.subscribe(topic):
        received.append(event)

    assert [event.kind for event in received] == ["done"]
    assert received[0].payload == {"ok": True}


def test_operation_stream_event_serializes_to_sse_payload() -> None:
    event = OperationStreamEvent.now(
        kind="repos_fetched",
        payload={"repos_imported": 2},
        correlation_id="github-sync-1",
    )

    payload = _to_sse_event(event)

    assert payload["event"] == "repos_fetched"
    assert '"repos_imported":2' in payload["data"]
    assert '"correlation_id":"github-sync-1"' in payload["data"]


def test_operation_stream_topic_helpers_are_stable() -> None:
    assert digest_run_topic("abc") == "digest:abc"
    assert github_sync_topic("abc") == "github-sync:abc"
    assert vector_reconcile_topic("abc") == "vector-reconcile:abc"


@pytest.mark.asyncio
async def test_vector_reconcile_body_publishes_scanned_requeued_and_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict]] = []

    def record_event(
        *, topic: str, kind: str, correlation_id: str, payload: dict | None = None
    ) -> None:
        events.append((kind, payload or {}))

    from app.tasks import reconcile_vector_index

    monkeypatch.setattr(reconcile_vector_index, "publish_operation_event", record_event)
    monkeypatch.setattr(
        reconcile_vector_index,
        "_fetch_stale_summaries",
        AsyncMock(return_value=[{"summary_id": 1, "request_id": 2, "json_payload": {}}]),
    )
    monkeypatch.setattr(
        reconcile_vector_index,
        "compute_vector_reconcile_oldest_lag_seconds",
        lambda rows: 12.0,
    )
    monkeypatch.setattr(
        reconcile_vector_index,
        "_build_runtime",
        lambda cfg, db: SimpleNamespace(
            embedding_generator=SimpleNamespace(
                generate_embeddings_for_summaries=AsyncMock(
                    return_value=SimpleNamespace(indexed=1, skipped=0, failed=0)
                )
            )
        ),
    )
    monkeypatch.setattr(reconcile_vector_index, "_sync_summary_vectors", AsyncMock(return_value=1))
    monkeypatch.setattr(reconcile_vector_index, "_record_reconcile_metrics", lambda *_, **__: None)

    cfg = SimpleNamespace(
        vector_reconcile=SimpleNamespace(enabled=True, batch_size=10),
    )

    summary = await reconcile_vector_index._reconcile_body(
        cfg,
        MagicMock(),
        correlation_id="vector-reconcile-test",
    )

    assert summary.scanned == 1
    assert ("rows_scanned", {"rows_scanned": 1, "oldest_lag_seconds": 12.0}) in events
    assert ("rows_requeued", {"rows_requeued": 1}) in events
    assert events[-1][0] == "done"


@pytest.mark.asyncio
async def test_github_sync_empty_run_publishes_phase_and_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[tuple[str, dict]] = []

    def record_event(
        *, topic: str, kind: str, correlation_id: str, payload: dict | None = None
    ) -> None:
        events.append((kind, payload or {}))

    from app.tasks import github_sync

    monkeypatch.setattr(github_sync, "publish_operation_event", record_event)
    monkeypatch.setattr(github_sync, "GITHUB_SYNC_RUNS_TOTAL", None)
    monkeypatch.setattr(github_sync, "GITHUB_PENDING_ANALYSIS_BACKLOG", None)

    summary = await github_sync._sync_all(
        [],
        cfg=MagicMock(),
        db=MagicMock(),
        correlation_id="github-sync-test",
    )

    assert summary.users_processed == 0
    assert events[0] == ("phase", {"phase": "starting", "integrations": 0})
    assert events[-1][0] == "done"
    assert events[-1][1]["users_processed"] == 0


def test_publish_operation_event_uses_global_hub() -> None:
    publish_operation_event(
        topic="smoke:topic",
        kind="phase",
        correlation_id="cid",
        payload={"phase": "smoke"},
    )
