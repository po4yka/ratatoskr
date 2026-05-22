"""Typed request/response models for summarization services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.adapters.content.url_flow_models import PhaseChangeCallback
    from app.core.telegram_progress_message import TelegramProgressMessage


@dataclass(frozen=True, slots=True)
class InteractiveSummaryRequest:
    """Inputs for Telegram-facing summary generation."""

    message: Any
    content_text: str
    chosen_lang: str
    system_prompt: str
    req_id: int
    max_chars: int
    correlation_id: str | None = None
    interaction_id: int | None = None
    url_hash: str | None = None
    url: str | None = None
    silent: bool = False
    defer_persistence: bool = False
    on_phase_change: PhaseChangeCallback | None = None
    images: list[str] | None = None
    progress_tracker: TelegramProgressMessage | None = None
    source_coverage: str | None = None
    extraction_quality: str | None = None
    extraction_confidence: float | None = None


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
    source_coverage: str | None = None
    extraction_quality: str | None = None
    extraction_confidence: float | None = None


@dataclass(frozen=True, slots=True)
class EnsureSummaryPayloadRequest:
    """Inputs for summary normalization and metadata enrichment."""

    summary: dict[str, Any]
    req_id: int
    content_text: str
    chosen_lang: str
    correlation_id: str | None = None
    source_coverage: str | None = None
    extraction_quality: str | None = None
    extraction_confidence: float | None = None
