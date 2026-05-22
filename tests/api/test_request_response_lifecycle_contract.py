"""Golden contract tests for public request lifecycle response vocabulary."""

from __future__ import annotations

from app.api.models.responses import (
    ProcessingStage,
    RequestDetailRequest,
    RequestStatus,
    RequestStatusData,
    SubmitRequestResponse,
)


def test_submit_response_uses_canonical_status_and_preserves_legacy_status() -> None:
    payload = SubmitRequestResponse(
        request_id=12,
        correlation_id="cid-submit",
        type="url",
        status=RequestStatus.PENDING,
        legacy_status="pending",
        estimated_wait_seconds=15,
        created_at="2026-05-21T00:00:00Z",
    ).model_dump(by_alias=True)

    assert payload["status"] == "pending"
    assert payload["legacyStatus"] == "pending"
    assert "processing" not in payload.values()


def test_status_response_uses_canonical_status_stage_and_legacy_status() -> None:
    payload = RequestStatusData(
        request_id=12,
        status=RequestStatus.RUNNING,
        legacy_status="processing",
        stage=ProcessingStage.SUMMARIZING,
        progress={"current_step": 2, "total_steps": 3, "percentage": 66},
        can_retry=False,
        updated_at="2026-05-21T00:00:00Z",
    ).model_dump(by_alias=True)

    assert payload["status"] == "running"
    assert payload["legacyStatus"] == "processing"
    assert payload["stage"] == "summarizing"
    assert payload["progress"]["percentage"] == 66
    assert "debug" not in payload


def test_detail_response_request_uses_canonical_status_and_legacy_status() -> None:
    payload = RequestDetailRequest(
        id=12,
        type="url",
        status=RequestStatus.SUCCEEDED,
        legacy_status="complete",
        correlation_id="cid-detail",
        created_at="2026-05-21T00:00:00Z",
    ).model_dump(by_alias=True)

    assert payload["status"] == "succeeded"
    assert payload["legacyStatus"] == "complete"
