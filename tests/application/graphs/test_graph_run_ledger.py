"""Regression coverage for durable, content-free graph node chronology."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.application.graphs.summarize.nodes._span import graph_node


class _Ledger:
    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    async def record_node(self, **kwargs: object) -> None:
        self.records.append(kwargs)


@pytest.mark.asyncio
async def test_graph_node_records_structural_lifecycle_without_state_content() -> None:
    ledger = _Ledger()

    @graph_node("validate")
    async def node(state, *, deps):  # type: ignore[no-untyped-def]
        return {"valid": True}

    result = await node(
        {
            "request_id": 91,
            "correlation_id": "cid-91",
            "source_text": "private source text",
            "messages": [{"content": "private prompt"}],
        },
        deps=SimpleNamespace(graph_run_ledger=ledger),
    )

    assert result == {"valid": True}
    assert ledger.records == [
        {"request_id": 91, "correlation_id": "cid-91", "node": "validate", "status": "started"},
        {
            "request_id": 91,
            "correlation_id": "cid-91",
            "node": "validate",
            "status": "completed",
        },
    ]
