"""Response formatting facade.

ResponseFormatter is the public Telegram-output API for Ratatoskr. It composes
specialized components and exposes a single stable surface used across
Telegram adapters, API handlers, and the DI container:

- DataFormatterImpl: Stateless data formatting (bytes, metrics, stats)
- MessageValidatorImpl: Security validation (content safety, URL validation, rate limiting)
- ResponseSenderImpl: Core Telegram sending (safe_reply, edit_message, reply_json)
- TextProcessorImpl: Text processing (chunking, sanitization, slugify)
- NotificationFormatterImpl: Status notifications (20+ notification methods)
- SummaryPresenterImpl: Summary presentation (structured summaries, translations)
- DatabasePresenterImpl: Database UI (overview, verification, search results)

Component protocols (ResponseSender, NotificationFormatter, etc.) live in
`app/adapters/external/formatting/protocols.py` for use in type hints when a
caller needs only one capability rather than the full facade.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from app.adapters.external.formatting.data_formatter import DataFormatterImpl
from app.adapters.external.formatting.database_presenter import DatabasePresenterImpl
from app.adapters.external.formatting.message_validator import MessageValidatorImpl
from app.adapters.external.formatting.notification_formatter import NotificationFormatterImpl
from app.adapters.external.formatting.response_sender import ResponseSenderImpl
from app.adapters.external.formatting.services import FormattingServices
from app.adapters.external.formatting.summary_presenter import SummaryPresenterImpl
from app.adapters.external.formatting.text_processor import TextProcessorImpl
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from app.adapters.external.formatting.protocols import (
        DatabasePresenter,
        DataFormatter,
        MessageValidator,
        NotificationFormatter,
        SummaryPresenter,
        TextProcessor,
    )
    from app.adapters.telegram.topic_manager import TopicManager
    from app.application.services.topic_search import TopicArticle
    from app.core.telegram_progress_message import TelegramProgressMessage
    from app.core.verbosity import VerbosityResolver

logger = get_logger(__name__)


class ResponseFormatter:
    """Handles message formatting and replies to Telegram users.

    Facade that composes specialized formatting/sending components behind a
    single stable interface.
    """

    def __init__(
        self,
        safe_reply_func: Callable[[Any, str], Awaitable[None]] | None = None,
        reply_json_func: Callable[[Any, dict[str, Any]], Awaitable[None]] | None = None,
        telegram_client: Any = None,
        telegram_limits: Any = None,
        telegram_config: Any = None,
        verbosity_resolver: VerbosityResolver | None = None,
        admin_log_chat_id: int | None = None,
        topic_manager: TopicManager | None = None,
        lang: str = "en",
    ) -> None:
        self._verbosity_resolver = verbosity_resolver
        self._lang = lang

        if telegram_limits is not None:
            self.MAX_MESSAGE_CHARS = telegram_limits.max_message_chars
            self.MAX_URL_LENGTH = telegram_limits.max_url_length
            self.MAX_BATCH_URLS = telegram_limits.max_batch_urls
            self.MIN_MESSAGE_INTERVAL_MS = telegram_limits.min_message_interval_ms
        else:
            self.MAX_MESSAGE_CHARS = 3500
            self.MAX_URL_LENGTH = 2048
            self.MAX_BATCH_URLS = 200
            self.MIN_MESSAGE_INTERVAL_MS = 100

        # Initialize components
        data_formatter: DataFormatter = DataFormatterImpl(lang=lang)

        message_validator: MessageValidator = MessageValidatorImpl(
            min_message_interval_ms=self.MIN_MESSAGE_INTERVAL_MS
        )

        response_sender = ResponseSenderImpl(
            message_validator,
            max_message_chars=self.MAX_MESSAGE_CHARS,
            safe_reply_func=safe_reply_func,
            reply_json_func=reply_json_func,
            telegram_client=telegram_client,
            admin_log_chat_id=admin_log_chat_id,
            draft_streaming_enabled=bool(getattr(telegram_config, "draft_streaming_enabled", True)),
            draft_min_interval_ms=int(getattr(telegram_config, "draft_min_interval_ms", 700)),
            draft_min_delta_chars=int(getattr(telegram_config, "draft_min_delta_chars", 40)),
            draft_max_chars=int(
                getattr(telegram_config, "draft_max_chars", self.MAX_MESSAGE_CHARS)
            ),
        )

        text_processor: TextProcessor = TextProcessorImpl(
            response_sender,
            max_message_chars=self.MAX_MESSAGE_CHARS,
        )

        # Create progress tracker for Reader mode
        from app.core.telegram_progress_message import TelegramProgressMessage

        progress_tracker = TelegramProgressMessage(response_sender)

        notification_formatter: NotificationFormatter = NotificationFormatterImpl(
            response_sender,
            data_formatter,
            verbosity_resolver=verbosity_resolver,
            progress_tracker=progress_tracker,
            lang=lang,
        )

        summary_presenter: SummaryPresenter = SummaryPresenterImpl(
            response_sender,
            text_processor,
            data_formatter,
            verbosity_resolver=verbosity_resolver,
            progress_tracker=progress_tracker,
            topic_manager=topic_manager,
            lang=lang,
        )

        database_presenter: DatabasePresenter = DatabasePresenterImpl(
            response_sender,
            data_formatter,
        )

        self._services = FormattingServices(
            sender=response_sender,
            notifications=notification_formatter,
            summaries=summary_presenter,
            database=database_presenter,
            validator=message_validator,
            text_processor=text_processor,
            data_formatter=data_formatter,
            progress_tracker=progress_tracker,
        )

        self._last_message_time: float = 0.0
        self._notified_error_ids: set[str] = set()

    @property
    def progress_tracker(self) -> TelegramProgressMessage:
        """Expose progress tracker for single-URL progress messages."""
        return self._services.progress_tracker

    @property
    def _response_sender(self) -> ResponseSenderImpl:
        return cast("ResponseSenderImpl", self._services.sender)

    @property
    def _notification_formatter(self) -> NotificationFormatter:
        return self._services.notifications

    @property
    def _summary_presenter(self) -> SummaryPresenterImpl:
        return cast("SummaryPresenterImpl", self._services.summaries)

    @property
    def _database_presenter(self) -> DatabasePresenter:
        return self._services.database

    @property
    def _message_validator(self) -> MessageValidator:
        return self._services.validator

    @property
    def _text_processor(self) -> TextProcessor:
        return self._services.text_processor

    @property
    def _data_formatter(self) -> DataFormatter:
        return self._services.data_formatter

    # ======== Sync configuration / mutation methods ========

    def set_telegram_client(self, telegram_client: Any) -> None:
        """Rebind Telegram transport dependencies for runtime wiring and tests."""
        self._response_sender.set_telegram_client(telegram_client)

    def set_reply_callbacks(
        self,
        *,
        safe_reply_func: Any = ...,
        reply_json_func: Any = ...,
    ) -> None:
        """Update transport callback overrides without mutating internals."""
        self._response_sender.set_reply_callbacks(
            safe_reply_func=safe_reply_func,
            reply_json_func=reply_json_func,
        )

    def set_topic_manager(self, topic_manager: TopicManager | None) -> None:
        """Rebind forum-topic routing without mutating presenter internals."""
        self._summary_presenter.set_topic_manager(topic_manager)

    # ======== Async public methods ========

    async def is_reader_mode(self, message: Any) -> bool:
        """Return True when the user prefers Reader (consolidated) UX."""
        if self._verbosity_resolver is None:
            return False
        try:
            from app.core.verbosity import VerbosityLevel

            return (await self._verbosity_resolver.get_verbosity(message)) == VerbosityLevel.READER
        except Exception:
            logger.debug("verbosity_level_import_failed", exc_info=True)
            return False

    # =========================================================================
    # ResponseSender delegation (core Telegram sending)
    # =========================================================================

    async def safe_reply(
        self, message: Any, text: str, *, parse_mode: str | None = None, reply_markup: Any = None
    ) -> None:
        """Safely reply to a message with comprehensive security checks."""
        await self._response_sender.safe_reply(
            message, text, parse_mode=parse_mode, reply_markup=reply_markup
        )

    async def safe_reply_with_id(
        self,
        message: Any,
        text: str,
        *,
        parse_mode: str | None = None,
        message_thread_id: int | None = None,
    ) -> int | None:
        """Safely reply to a message and return the message ID."""
        return await self._response_sender.safe_reply_with_id(
            message,
            text,
            parse_mode=parse_mode,
            message_thread_id=message_thread_id,
        )

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, *, parse_mode: str | None = None
    ) -> bool:
        """Edit an existing message in Telegram with security checks."""
        return await self._response_sender.edit_message(
            chat_id, message_id, text, parse_mode=parse_mode
        )

    async def react(self, chat_id: int, message_id: int, emoji: str) -> bool:
        """React to a message with an emoji (best-effort status ack)."""
        return await self._response_sender.react(chat_id, message_id, emoji)

    async def send_chat_action(
        self,
        chat_id: int,
        action: str = "typing",
    ) -> bool:
        return await self._response_sender.send_chat_action(chat_id, action)

    async def send_message_draft(
        self,
        message: Any,
        text: str,
        *,
        message_thread_id: int | None = None,
        force: bool = False,
    ) -> bool:
        """Send Telegram draft update with automatic per-request fallback detection."""
        return await self._response_sender.send_message_draft(
            message,
            text,
            message_thread_id=message_thread_id,
            force=force,
        )

    def clear_message_draft(self, message: Any) -> None:
        """Clear request-scoped draft state."""
        self._response_sender.clear_message_draft(message)

    def is_draft_streaming_enabled(self) -> bool:
        """Return whether Telegram draft streaming is enabled."""
        return self._response_sender.is_draft_streaming_enabled()

    async def reply_json(
        self,
        message: Any,
        obj: dict[str, Any],
        *,
        correlation_id: str | None = None,
        success: bool = True,
    ) -> None:
        """Reply with JSON object, using file upload for large content."""
        await self._response_sender.reply_json(
            message, obj, correlation_id=correlation_id, success=success
        )

    def create_inline_keyboard(self, buttons: list[dict[str, str]]) -> Any:
        """Create an inline keyboard markup from button definitions."""
        return self._response_sender.create_inline_keyboard(buttons)

    # =========================================================================
    # NotificationFormatter delegation (status notifications)
    # =========================================================================

    async def send_help(self, message: Any) -> None:
        await self._notification_formatter.send_help(message)

    async def send_welcome(self, message: Any) -> None:
        await self._notification_formatter.send_welcome(message)

    async def send_url_accepted_notification(
        self, message: Any, norm: str, correlation_id: str, *, silent: bool = False
    ) -> None:
        """Send URL accepted notification."""
        await self._notification_formatter.send_url_accepted_notification(
            message, norm, correlation_id, silent=silent
        )

    async def send_firecrawl_start_notification(
        self, message: Any, url: str | None = None, *, silent: bool = False
    ) -> None:
        """Send Firecrawl start notification."""
        await self._notification_formatter.send_firecrawl_start_notification(
            message, url, silent=silent
        )

    async def send_firecrawl_success_notification(
        self,
        message: Any,
        excerpt_len: int,
        latency_sec: float,
        *,
        http_status: int | None = None,
        crawl_status: str | None = None,
        correlation_id: str | None = None,
        endpoint: str | None = None,
        options: dict[str, Any] | None = None,
        silent: bool = False,
    ) -> None:
        """Send Firecrawl success notification with crawl metadata."""
        await self._notification_formatter.send_firecrawl_success_notification(
            message,
            excerpt_len,
            latency_sec,
            http_status=http_status,
            crawl_status=crawl_status,
            correlation_id=correlation_id,
            endpoint=endpoint,
            options=options,
            silent=silent,
        )

    async def send_content_reuse_notification(
        self,
        message: Any,
        *,
        http_status: int | None = None,
        crawl_status: str | None = None,
        latency_sec: float | None = None,
        correlation_id: str | None = None,
        options: dict[str, Any] | None = None,
        silent: bool = False,
    ) -> None:
        """Send content reuse notification with cached crawl metadata."""
        await self._notification_formatter.send_content_reuse_notification(
            message,
            http_status=http_status,
            crawl_status=crawl_status,
            latency_sec=latency_sec,
            correlation_id=correlation_id,
            options=options,
            silent=silent,
        )

    async def send_cached_summary_notification(self, message: Any, *, silent: bool = False) -> None:
        """Inform the user that a cached summary is being reused."""
        await self._notification_formatter.send_cached_summary_notification(message, silent=silent)

    async def send_html_fallback_notification(
        self, message: Any, content_len: int, *, silent: bool = False
    ) -> None:
        """Send HTML fallback notification."""
        await self._notification_formatter.send_html_fallback_notification(
            message, content_len, silent=silent
        )

    async def send_language_detection_notification(
        self,
        message: Any,
        detected: str | None,
        content_preview: str,
        *,
        url: str | None = None,
        silent: bool = False,
    ) -> None:
        """Send language detection notification."""
        await self._notification_formatter.send_language_detection_notification(
            message, detected, content_preview, url=url, silent=silent
        )

    async def send_content_analysis_notification(
        self,
        message: Any,
        content_len: int,
        max_chars: int,
        enable_chunking: bool,
        chunks: list[str] | None,
        structured_output_mode: str,
        *,
        silent: bool = False,
    ) -> None:
        """Send content analysis notification."""
        await self._notification_formatter.send_content_analysis_notification(
            message,
            content_len,
            max_chars,
            enable_chunking,
            chunks,
            structured_output_mode,
            silent=silent,
        )

    async def send_llm_start_notification(
        self,
        message: Any,
        model: str,
        content_len: int,
        structured_output_mode: str,
        *,
        url: str | None = None,
        silent: bool = False,
    ) -> None:
        """Send LLM start notification."""
        await self._notification_formatter.send_llm_start_notification(
            message, model, content_len, structured_output_mode, url=url, silent=silent
        )

    async def send_llm_completion_notification(
        self, message: Any, llm: Any, correlation_id: str, *, silent: bool = False
    ) -> None:
        """Send LLM completion notification."""
        await self._notification_formatter.send_llm_completion_notification(
            message, llm, correlation_id, silent=silent
        )

    async def send_forward_accepted_notification(self, message: Any, title: str) -> None:
        """Send forward request accepted notification."""
        await self._notification_formatter.send_forward_accepted_notification(message, title)

    async def send_forward_language_notification(self, message: Any, detected: str | None) -> None:
        """Send forward language detection notification."""
        await self._notification_formatter.send_forward_language_notification(message, detected)

    async def send_forward_completion_notification(self, message: Any, llm: Any) -> None:
        """Send forward completion notification."""
        await self._notification_formatter.send_forward_completion_notification(message, llm)

    async def send_youtube_download_notification(
        self, message: Any, url: str, *, silent: bool = False
    ) -> None:
        """Notify user that YouTube video download is starting."""
        await self._notification_formatter.send_youtube_download_notification(
            message, url, silent=silent
        )

    async def send_youtube_download_complete_notification(
        self,
        message: Any,
        title: str,
        resolution: str,
        size_mb: float,
        *,
        silent: bool = False,
    ) -> None:
        """Notify user that video download is complete."""
        await self._notification_formatter.send_youtube_download_complete_notification(
            message, title, resolution, size_mb, silent=silent
        )

    async def send_error_notification(
        self,
        message: Any,
        error_type: str,
        correlation_id: str,
        details: str | None = None,
        reply_markup: Any | None = None,
    ) -> None:
        """Send error notification with rich formatting."""
        await self._notification_formatter.send_error_notification(
            message, error_type, correlation_id, details, reply_markup=reply_markup
        )

    # =========================================================================
    # SummaryPresenter delegation (summary presentation)
    # =========================================================================

    async def send_structured_summary_response(
        self,
        message: Any,
        summary_shaped: dict[str, Any],
        llm: Any,
        chunks: int | None = None,
        summary_id: int | str | None = None,
        correlation_id: str | None = None,
    ) -> int | None:
        """Send summary where each top-level JSON field is a separate message.

        Returns the Telegram message ID of the last sent message, or None.
        """
        return await self._summary_presenter.send_structured_summary_response(
            message,
            summary_shaped,
            llm,
            chunks,
            summary_id=summary_id,
            correlation_id=correlation_id,
        )

    async def send_forward_summary_response(
        self, message: Any, forward_shaped: dict[str, Any], summary_id: int | str | None = None
    ) -> None:
        """Send forward summary with per-field messages."""
        await self._summary_presenter.send_forward_summary_response(
            message, forward_shaped, summary_id=summary_id
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
        """Render the full summary content in a second language (e.g. Russian)."""
        return await self._summary_presenter.send_secondary_language_summary(
            message,
            summary_shaped,
            lang=lang,
            header=header,
            correlation_id=correlation_id,
        )

    async def send_russian_translation(
        self, message: Any, translated_text: str, correlation_id: str | None = None
    ) -> None:
        """Send the adapted Russian translation as a follow-up message."""
        await self._summary_presenter.send_russian_translation(
            message, translated_text, correlation_id
        )

    async def send_additional_insights_message(
        self, message: Any, insights: dict[str, Any], correlation_id: str | None = None
    ) -> None:
        """Send follow-up message summarizing additional research insights."""
        await self._summary_presenter.send_additional_insights_message(
            message, insights, correlation_id
        )

    async def send_custom_article(self, message: Any, article: dict[str, Any]) -> None:
        """Send the custom generated article with a nice header and downloadable JSON."""
        await self._summary_presenter.send_custom_article(message, article)

    async def send_related_reads(
        self,
        message: Any,
        items: list[Any],
        *,
        lang: str | None = None,
    ) -> None:
        """Send related-read shortcuts as a follow-up keyboard."""
        await self._summary_presenter.send_related_reads(message, items, lang=lang)

    # =========================================================================
    # DatabasePresenter delegation (database UI)
    # =========================================================================

    async def send_db_overview(self, message: Any, overview: dict[str, object]) -> None:
        """Send an overview of the database state."""
        await self._database_presenter.send_db_overview(message, overview)

    async def send_topic_search_results(
        self,
        message: Any,
        *,
        topic: str,
        articles: Sequence[TopicArticle],
        source: str = "online",
    ) -> None:
        """Send a formatted list of topic search results to the user."""
        await self._database_presenter.send_topic_search_results(
            message, topic=topic, articles=articles, source=source
        )

    async def send_db_verification(self, message: Any, verification: dict[str, Any]) -> None:
        """Send database verification summary highlighting missing fields."""
        await self._database_presenter.send_db_verification(message, verification)

    async def send_db_reprocess_start(
        self,
        message: Any,
        *,
        url_targets: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
    ) -> None:
        """Notify the user that reprocessing of missing posts has started."""
        await self._database_presenter.send_db_reprocess_start(
            message, url_targets=url_targets, skipped=skipped
        )

    async def send_db_reprocess_complete(
        self,
        message: Any,
        *,
        url_targets: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
    ) -> None:
        """Summarize the outcome of the automated reprocessing."""
        await self._database_presenter.send_db_reprocess_complete(
            message, url_targets=url_targets, failures=failures, skipped=skipped
        )

    # =========================================================================
    # Private methods exposed for tests
    # =========================================================================

    def _validate_content(self, text: str) -> tuple[bool, str]:
        """Validate content for security issues."""
        return self._message_validator.validate_content(text)

    def _validate_url(self, url: str) -> tuple[bool, str]:
        """Validate URL for security."""
        return self._message_validator.validate_url(url)

    async def _check_rate_limit(self) -> bool:
        """Ensure replies respect the minimum delay between Telegram messages."""
        return await self._message_validator.check_rate_limit()

    def _chunk_text(self, text: str, *, max_len: int) -> list[str]:
        """Split text into chunks respecting Telegram's message length limit."""
        return self._text_processor.chunk_text(text, max_len=max_len)

    def _sanitize_summary_text(self, text: str) -> str:
        """Normalize and clean summary text for safe sending."""
        return self._text_processor.sanitize_summary_text(text)

    def _slugify(self, text: str, *, max_len: int = 60) -> str:
        """Create a filesystem-friendly slug from text."""
        return self._text_processor.slugify(text, max_len=max_len)

    def _build_json_filename(self, obj: dict[str, Any]) -> str:
        """Build a descriptive filename for the JSON attachment."""
        return self._text_processor.build_json_filename(obj)

    async def _send_long_text(self, message: Any, text: str) -> None:
        """Send text, splitting into multiple messages if too long for Telegram."""
        await self._text_processor.send_long_text(message, text)

    async def _send_labelled_text(self, message: Any, label: str, body: str) -> None:
        """Send labelled text, splitting into continuation messages when needed."""
        await self._text_processor.send_labelled_text(message, label, body)

    def _format_bytes(self, size: int) -> str:
        """Convert byte count into a human-readable string."""
        return self._data_formatter.format_bytes(size)

    def _format_metric_value(self, value: Any) -> str | None:
        """Format metric values, trimming insignificant decimals and booleans."""
        return self._data_formatter.format_metric_value(value)

    def _format_key_stats(self, key_stats: list[dict[str, Any]]) -> list[str]:
        """Render key statistics into bullet-point lines."""
        return self._data_formatter.format_key_stats(key_stats)

    def _format_readability(self, readability: Any) -> str | None:
        """Create a reader-friendly readability summary line."""
        return self._data_formatter.format_readability(readability)

    def _format_firecrawl_options(self, options: dict[str, Any] | None) -> str | None:
        """Format Firecrawl options into a display string."""
        return self._data_formatter.format_firecrawl_options(options)
