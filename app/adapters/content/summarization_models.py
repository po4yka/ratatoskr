"""Typed request/response models for summarization services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class InteractiveSummaryResult:
    """Result bundle for interactive summary execution."""

    summary: dict[str, Any] | None
    llm_result: Any | None
    served_from_cache: bool
    model_used: str | None


@dataclass(frozen=True, slots=True)
class PureSummaryRequest:
    """Inputs for non-message summarization."""

    content_text: str
    chosen_lang: str
    system_prompt: str
    correlation_id: str | None = None
    feedback_instructions: str | None = None
    request_id: int | None = None
    stream: bool = False
    source_coverage: str | None = None
    extraction_quality: str | None = None
    extraction_confidence: float | None = None
