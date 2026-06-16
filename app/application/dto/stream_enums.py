"""Neutral streaming-progress enum types.

These pure ``StrEnum`` definitions are shared between the adapter layer
(``app.adapters.content.streaming``) and the API layer
(``app.api.models.responses.common``).  They carry no API-runtime
dependencies so they are safe to import from any layer.

The API module re-exports ``ProcessingStage`` and ``ProgressEventKind``
from here, so existing API-layer importers need no changes.
"""

from __future__ import annotations

from enum import StrEnum

# Custom astream_events name the summarize node dispatches per summary token
# delta (ADR-0017). Lives here so the application node (producer) and the
# adapters streaming bridge (consumer) share ONE literal without crossing layers.
SUMMARY_TOKEN_EVENT = "summary_token"


class ProcessingStage(StrEnum):
    """Canonical public processing stages for request status polling and streams."""

    QUEUED = "queued"
    EXTRACTING = "extracting"
    SUMMARIZING = "summarizing"
    VALIDATING = "validating"
    PERSISTING = "persisting"
    DONE = "done"


class ProgressEventKind(StrEnum):
    """Canonical public SSE progress event kinds."""

    STAGE = "stage"
    SECTION = "section"
    WARNING = "warning"
    DONE = "done"
    ERROR = "error"


__all__ = ["SUMMARY_TOKEN_EVENT", "ProcessingStage", "ProgressEventKind"]
