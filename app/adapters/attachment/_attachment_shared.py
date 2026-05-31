"""Shared helpers and context for attachment processing."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.llm.protocol import LLMClientProtocol
    from app.application.ports.requests import RequestRepositoryPort
    from app.application.ports.users import UserRepositoryPort
    from app.application.services.summarization.llm_response_workflow import LLMResponseWorkflow
    from app.config import AppConfig
    from app.db.session import Database

_PROMPT_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"
_MAX_PDF_TEXT_CHARS = 45_000


def coerce_int(value: Any) -> int | None:
    """Convert a value to int when possible."""
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def load_prompt(prompt_name: str, lang: str) -> str:
    """Load a prompt file by name and language."""
    lang = lang if lang in ("en", "ru") else "en"
    path = _PROMPT_DIR / f"{prompt_name}_{lang}.txt"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        fallback = _PROMPT_DIR / f"{prompt_name}_en.txt"
        return fallback.read_text(encoding="utf-8").strip()


@dataclass(slots=True)
class AttachmentProcessorContext:
    """Shared runtime state for the attachment processor helpers."""

    cfg: AppConfig
    db: Database
    openrouter: LLMClientProtocol
    response_formatter: ResponseFormatter
    audit_func: Callable[[str, str, dict[str, Any]], None]
    sem: Callable[[], Any]
    request_repo: RequestRepositoryPort
    user_repo: UserRepositoryPort
    workflow: LLMResponseWorkflow
    logger: Any
