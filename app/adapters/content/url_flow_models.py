"""Shared models and helpers for URL-processing flows."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from app.core.call_status import CallStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.core.telegram_progress_message import TelegramProgressMessage

    PhaseChangeCallback = Callable[[str, str | None, int | None, str | None], Awaitable[None]]
else:  # pragma: no cover - runtime alias for typing-only callback
    PhaseChangeCallback = Any


@dataclass(slots=True)
class URLFlowRequest:
    """Request envelope for a single URL-processing flow."""

    message: Any
    url_text: str
    correlation_id: str | None = None
    interaction_id: int | None = None
    silent: bool = False
    batch_mode: bool = False
    on_phase_change: PhaseChangeCallback | None = None
    progress_tracker: TelegramProgressMessage | None = None

    @property
    def effective_silent(self) -> bool:
        return self.silent or self.batch_mode


@dataclass(slots=True)
class URLFlowContext:
    """Prepared context for URL extraction and summarization."""

    dedupe_hash: str
    req_id: int
    content_text: str
    title: str | None
    images: list[str] | None
    chosen_lang: str
    needs_ru_translation: bool
    system_prompt: str
    should_chunk: bool
    max_chars: int
    chunks: list[str] | None
    source_coverage: str = "unknown"
    extraction_quality: str | None = None
    extraction_confidence: float | None = None


@dataclass(slots=True)
class URLProcessingFlowResult:
    """Result of URL processing flow for batch status tracking."""

    success: bool = True
    title: str | None = None
    cached: bool = False
    summary_json: dict[str, Any] | None = None
    request_id: int | None = None

    @classmethod
    def from_summary(
        cls,
        summary_json: dict[str, Any] | None,
        cached: bool = False,
        request_id: int | None = None,
    ) -> URLProcessingFlowResult:
        """Create a flow result from summary JSON, extracting a title preview."""
        if not summary_json:
            return cls(success=True, title=None, cached=cached)

        title = _extract_summary_title(summary_json)
        return cls(
            success=True,
            title=title,
            cached=cached,
            summary_json=summary_json,
            request_id=request_id,
        )


def create_chunk_llm_stub(cfg: Any) -> Any:
    """Create a lightweight LLM result stub for cached/chunked responses."""
    return SimpleNamespace(
        status=CallStatus.OK,
        latency_ms=None,
        model=cfg.openrouter.model,
        cost_usd=None,
        tokens_prompt=None,
        tokens_completion=None,
        structured_output_used=True,
        structured_output_mode=cfg.openrouter.structured_output_mode,
    )


def _extract_summary_title(summary_json: dict[str, Any]) -> str | None:
    title = None
    if "title" in summary_json:
        title = str(summary_json["title"])[:100]

    if not title and summary_json.get("summary_250"):
        title = str(summary_json["summary_250"])
        if len(title) > 60:
            for sep in (". ", "! ", "? "):
                idx = title.find(sep)
                if 0 < idx < 60:
                    title = title[: idx + 1]
                    break
            else:
                title = title[:57] + "..."

    if not title and summary_json.get("tldr"):
        title = str(summary_json["tldr"])
        if len(title) > 60:
            title = title[:57] + "..."

    return title
