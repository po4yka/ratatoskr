"""Summary presentation formatting."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.external.formatting.summary.followup_presenters import (
    SummaryFollowupPresenters,
)
from app.adapters.external.formatting.summary.presenter_context import SummaryPresenterContext
from app.adapters.external.formatting.summary.structured_summary_flow import (
    StructuredSummaryFlow,
)
from app.adapters.external.formatting.summary.summary_blocks import SummaryBlocksPresenter

if TYPE_CHECKING:
    from app.adapters.external.formatting.protocols import (
        DataFormatter,
        ResponseSender,
        TextProcessor,
    )
    from app.adapters.telegram.topic_manager import TopicManager
    from app.application.services.related_reads_service import RelatedReadItem
    from app.core.telegram_progress_message import TelegramProgressMessage
    from app.core.verbosity import VerbosityResolver


class SummaryPresenterImpl:
    """Implementation of summary presentation."""

    def __init__(
        self,
        response_sender: ResponseSender,
        text_processor: TextProcessor,
        data_formatter: DataFormatter,
        *,
        verbosity_resolver: VerbosityResolver | None = None,
        progress_tracker: TelegramProgressMessage | None = None,
        topic_manager: TopicManager | None = None,
        lang: str = "en",
    ) -> None:
        self._context = SummaryPresenterContext(
            response_sender=response_sender,
            text_processor=text_processor,
            data_formatter=data_formatter,
            verbosity_resolver=verbosity_resolver,
            progress_tracker=progress_tracker,
            topic_manager=topic_manager,
            lang=lang,
        )
        self._blocks = SummaryBlocksPresenter(self._context)
        self._structured = StructuredSummaryFlow(self._context, blocks=self._blocks)
        self._followups = SummaryFollowupPresenters(self._context)

    def set_topic_manager(self, topic_manager: TopicManager | None) -> None:
        self._context.topic_manager = topic_manager

    async def send_structured_summary_response(
        self,
        message: Any,
        summary_shaped: dict[str, Any],
        llm: Any,
        chunks: int | None = None,
        summary_id: int | str | None = None,
        correlation_id: str | None = None,
    ) -> int | None:
        return await self._structured.send_structured_summary_response(
            message,
            summary_shaped,
            llm,
            chunks=chunks,
            summary_id=summary_id,
            correlation_id=correlation_id,
        )

    async def send_secondary_language_summary(
        self,
        message: Any,
        summary_shaped: dict[str, Any],
        *,
        lang: str,
        header: str | None = None,
        correlation_id: str | None = None,
    ) -> bool:
        return await self._structured.send_secondary_language_summary(
            message,
            summary_shaped,
            lang=lang,
            header=header,
            correlation_id=correlation_id,
        )

    async def send_forward_summary_response(
        self, message: Any, forward_shaped: dict[str, Any], summary_id: int | str | None = None
    ) -> None:
        await self._structured.send_forward_summary_response(
            message,
            forward_shaped,
            summary_id=summary_id,
        )

    async def send_russian_translation(
        self, message: Any, translated_text: str, correlation_id: str | None = None
    ) -> None:
        await self._followups.send_russian_translation(
            message,
            translated_text,
            correlation_id=correlation_id,
        )

    async def send_additional_insights_message(
        self, message: Any, insights: dict[str, Any], correlation_id: str | None = None
    ) -> None:
        await self._followups.send_additional_insights_message(
            message,
            insights,
            correlation_id=correlation_id,
        )

    async def send_custom_article(self, message: Any, article: dict[str, Any]) -> None:
        await self._followups.send_custom_article(message, article)

    async def send_related_reads(
        self,
        message: Any,
        items: list[RelatedReadItem],
        *,
        lang: str | None = None,
    ) -> None:
        await self._followups.send_related_reads(message, items, lang=lang)
