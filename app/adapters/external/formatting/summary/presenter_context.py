"""Shared context for summary-presentation helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        DataFormatter,
        ResponseSender,
        TextProcessor,
    )
    from app.core.telegram_progress_message import TelegramProgressMessage
    from app.core.verbosity import VerbosityResolver


@dataclass(slots=True)
class SummaryPresenterContext:
    """Mutable collaborator bundle shared by summary helpers."""

    response_sender: ResponseSender
    text_processor: TextProcessor
    data_formatter: DataFormatter
    verbosity_resolver: VerbosityResolver | None
    progress_tracker: TelegramProgressMessage | None
    lang: str
