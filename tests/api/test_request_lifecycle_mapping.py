from __future__ import annotations

import pytest

from app.application.dto.request_lifecycle import (
    progress_event_kind,
    project_request_lifecycle,
    public_processing_stage,
    public_request_status,
)


@pytest.mark.parametrize(
    ("legacy_status", "public_status", "public_stage", "event_kind"),
    [
        ("pending", "pending", "queued", "stage"),
        ("queued", "pending", "queued", "stage"),
        ("running", "running", "queued", "stage"),
        ("processing", "running", "summarizing", "stage"),
        ("crawling", "running", "extracting", "stage"),
        ("summarizing", "running", "summarizing", "stage"),
        ("success", "succeeded", "done", "done"),
        ("succeeded", "succeeded", "done", "done"),
        ("complete", "succeeded", "done", "done"),
        ("completed", "succeeded", "done", "done"),
        ("ok", "succeeded", "done", "done"),
        ("error", "failed", "done", "error"),
        ("failed", "failed", "done", "error"),
        ("cancelled", "cancelled", "done", "error"),
        ("x_imported", "succeeded", "done", "done"),
    ],
)
def test_request_lifecycle_mapper_covers_every_legacy_status(
    legacy_status: str,
    public_status: str,
    public_stage: str,
    event_kind: str,
) -> None:
    projection = project_request_lifecycle(status=legacy_status, stage=legacy_status)

    assert public_request_status(legacy_status) == public_status
    assert projection.status == public_status
    assert projection.stage == public_stage
    assert progress_event_kind(legacy_status) == event_kind


@pytest.mark.parametrize(
    ("legacy_stage", "public_stage"),
    [
        ("extraction", "extracting"),
        ("extracting", "extracting"),
        ("summarization", "summarizing"),
        ("summarizing", "summarizing"),
        ("validation", "validating"),
        ("validating", "validating"),
        ("saving", "persisting"),
        ("persisting", "persisting"),
        ("unknown", "done"),
    ],
)
def test_request_lifecycle_mapper_covers_stage_aliases(
    legacy_stage: str,
    public_stage: str,
) -> None:
    assert public_processing_stage(legacy_stage) == public_stage
