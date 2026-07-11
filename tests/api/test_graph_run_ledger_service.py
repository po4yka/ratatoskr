"""Pure projection tests for the graph-run privacy boundary."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from app.api.services.graph_run_ledger_service import build_graph_run_ledger
from app.core.time_utils import UTC


def test_graph_run_ledger_allowlists_operational_fields_and_feedback_labels() -> None:
    now = dt.datetime.now(UTC)
    ledger = build_graph_run_ledger(
        request=SimpleNamespace(
            id=71,
            status="completed",
            created_at=now,
            processing_time_ms=900,
            input_url="https://example.com/private?token=raw-url-token",
            content_text="private article body",
        ),
        events=[
            SimpleNamespace(
                sequence=1,
                kind="stage",
                stage="build_prompt",
                status="running",
                message="private raw prompt",
                payload={"prompt": "private raw prompt"},
                created_at=now,
            )
        ],
        calls=[
            SimpleNamespace(
                attempt_index=1,
                attempt_trigger="initial",
                provider="openrouter",
                model="test/model",
                status="error",
                latency_ms=100,
                total_latency_ms=120,
                tokens_prompt=10,
                tokens_completion=20,
                cost_usd=0.01,
                fallback_model_used="fallback/model",
                retry_exhausted=True,
                error_text="private provider error secret=raw-error",
                request_messages_json=[{"content": "private raw prompt"}],
                response_text="private raw completion",
            ),
            SimpleNamespace(
                attempt_index=2,
                attempt_trigger="repair_loop",
                provider="openrouter",
                model="test/model",
                status="success",
                latency_ms=200,
                total_latency_ms=None,
                tokens_prompt=30,
                tokens_completion=40,
                cost_usd=0.02,
                fallback_model_used=None,
                retry_exhausted=False,
                error_text=None,
            ),
        ],
        feedback=[
            SimpleNamespace(
                rating=1,
                issues='["wrong_scope", "private issue secret"]',
                comment="private free-form feedback",
                updated_at=now,
            )
        ],
    )

    assert ledger.metrics.node_count == 1
    assert ledger.metrics.repair_count == 1
    assert ledger.metrics.fallback_count == 1
    assert ledger.metrics.llm_latency_ms == 300
    assert ledger.metrics.total_cost_usd == 0.03
    assert ledger.feedback.rating_average == 1.0
    assert ledger.feedback.issue_count == 2
    assert ledger.attempts[0].error_present is True
    assert ledger.chronology[0].stage == "build_prompt"

    rendered = ledger.model_dump_json(by_alias=True)
    for secret in (
        "raw-url-token",
        "private article body",
        "private raw prompt",
        "private raw completion",
        "raw-error",
        "private issue secret",
        "private free-form feedback",
    ):
        assert secret not in rendered
