"""Shared public request lifecycle projection helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProgressEventKind = Literal["stage", "done", "error"]

PUBLIC_REQUEST_STATUS_BY_LEGACY: dict[str, str] = {
    "pending": "pending",
    "queued": "pending",
    "running": "running",
    "processing": "running",
    "crawling": "running",
    "summarizing": "running",
    "success": "succeeded",
    "succeeded": "succeeded",
    "complete": "succeeded",
    "completed": "succeeded",
    "ok": "succeeded",
    "fieldtheory_imported": "succeeded",
    "error": "failed",
    "failed": "failed",
    "cancelled": "cancelled",
}

PUBLIC_PROCESSING_STAGE_BY_LEGACY: dict[str, str] = {
    "pending": "queued",
    "queued": "queued",
    "crawling": "extracting",
    "extraction": "extracting",
    "extracting": "extracting",
    "processing": "summarizing",
    "summarization": "summarizing",
    "summarizing": "summarizing",
    "validation": "validating",
    "validating": "validating",
    "saving": "persisting",
    "persisting": "persisting",
    "success": "done",
    "succeeded": "done",
    "complete": "done",
    "completed": "done",
    "ok": "done",
    "done": "done",
    "fieldtheory_imported": "done",
    "unknown": "done",
    "error": "done",
    "failed": "done",
    "cancelled": "done",
}


@dataclass(frozen=True, slots=True)
class RequestLifecycleProjection:
    status: str
    stage: str


def public_request_status(status: object) -> str:
    """Return the canonical public status for a legacy/internal status value."""
    return PUBLIC_REQUEST_STATUS_BY_LEGACY.get(_normalize_key(status), "pending")


def public_processing_stage(stage: object, *, default: str = "queued") -> str:
    """Return the canonical public processing stage for a legacy/internal stage value."""
    return PUBLIC_PROCESSING_STAGE_BY_LEGACY.get(_normalize_key(stage), default)


def project_request_lifecycle(
    *,
    status: object,
    stage: object,
    default_stage: str = "queued",
) -> RequestLifecycleProjection:
    """Project legacy/internal lifecycle values into public API vocabulary."""
    return RequestLifecycleProjection(
        status=public_request_status(status),
        stage=public_processing_stage(stage, default=default_stage),
    )


def progress_event_kind(status: object) -> ProgressEventKind:
    """Return the SSE event kind implied by a public or legacy request status."""
    public_status = public_request_status(status)
    if public_status == "succeeded":
        return "done"
    if public_status in {"failed", "cancelled"}:
        return "error"
    return "stage"


def _normalize_key(value: object) -> str:
    return str(value or "").lower()
