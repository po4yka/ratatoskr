"""Protocol definitions for response formatting components."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from app.application.services.related_reads_service import RelatedReadItem
    from app.application.services.topic_search import TopicArticle


@runtime_checkable
class DataFormatter(Protocol):
    """Protocol for stateless data formatting operations."""

    def format_bytes(self, size: int) -> str:
        """Convert byte count into a human-readable string."""
        ...

    def format_metric_value(self, value: Any) -> str | None:
        """Format metric values, trimming insignificant decimals and booleans."""
        ...

    def format_key_stats(self, key_stats: list[dict[str, Any]]) -> list[str]:
        """Render key statistics into bullet-point lines."""
        ...

    def format_readability(self, readability: Any) -> str | None:
        """Create a reader-friendly readability summary line."""
        ...

    def format_firecrawl_options(self, options: dict[str, Any] | None) -> str | None:
        """Format Firecrawl options into a display string."""
        ...


@runtime_checkable
class MessageValidator(Protocol):
    """Protocol for message security validation."""

    def validate_content(self, text: str) -> tuple[bool, str]:
        """Validate content for security issues."""
        ...

    def validate_url(self, url: str) -> tuple[bool, str]:
        """Validate URL for security using consolidated validation."""
        ...

    async def check_rate_limit(self) -> bool:
        """Ensure replies respect the minimum delay between Telegram messages."""
        ...


@runtime_checkable
class ResponseSender(Protocol):
    """Protocol for core Telegram message sending."""

    async def safe_reply(
        self,
        message: Any,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        disable_web_page_preview: bool | None = None,
        message_thread_id: int | None = None,
        silent: bool = False,
    ) -> None:
        """Safely reply to a message with comprehensive security checks."""
        ...

    async def safe_reply_with_id(
        self,
        message: Any,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any = None,
        disable_web_page_preview: bool | None = None,
        message_thread_id: int | None = None,
    ) -> int | None:
        """Safely reply to a message and return the message ID."""
        ...

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        disable_web_page_preview: bool | None = None,
    ) -> bool:
        """Edit an existing message in Telegram with security checks."""
        ...

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        """Send chat action (typing/upload indicator) to Telegram."""
        ...

    async def reply_json(
        self,
        message: Any,
        obj: dict[str, Any],
        *,
        correlation_id: str | None = None,
        success: bool = True,
    ) -> None:
        """Reply with JSON object, using file upload for large content."""
        ...

    async def send_message_draft(
        self,
        message: Any,
        text: str,
        *,
        message_thread_id: int | None = None,
        force: bool = False,
    ) -> bool:
        """Send a Telegram draft update if enabled."""
        ...

    def clear_message_draft(self, message: Any) -> None:
        """Clear request-level draft stream state."""
        ...

    def is_draft_streaming_enabled(self) -> bool:
        """Return whether draft-stream sending is enabled."""
        ...

    def set_telegram_client(self, telegram_client: Any) -> None:
        """Inject/replace telegram client dependency after construction."""
        ...

    def create_inline_keyboard(self, buttons: list[dict[str, str]]) -> Any:
        """Create an inline keyboard markup from button definitions."""
        ...

    async def send_to_admin_log(self, text: str, *, correlation_id: str | None = None) -> None:
        """Forward diagnostic text to admin log chat when configured."""
        ...


@runtime_checkable
class TextProcessor(Protocol):
    """Protocol for text processing and chunking."""

    @property
    def max_message_chars(self) -> int:
        """Per-message character ceiling used for splitting."""
        ...

    def chunk_text(self, text: str, *, max_len: int) -> list[str]:
        """Split text into chunks respecting Telegram's message length limit."""
        ...

    def sanitize_summary_text(self, text: str) -> str:
        """Normalize and clean summary text for safe sending."""
        ...

    def slugify(self, text: str, *, max_len: int = 60) -> str:
        """Create a filesystem-friendly slug from text."""
        ...

    def build_json_filename(self, obj: dict[str, Any]) -> str:
        """Build a descriptive filename for the JSON attachment."""
        ...

    def linkify_urls(self, text: str) -> str:
        """Convert bare URLs in text to clickable HTML links."""
        ...

    async def send_long_text(
        self,
        message: Any,
        text: str,
        *,
        parse_mode: str | None = None,
    ) -> None:
        """Send text, splitting into multiple messages if too long for Telegram."""
        ...

    async def send_labelled_text(self, message: Any, label: str, body: str) -> None:
        """Send labelled text, splitting into continuation messages when needed."""
        ...


@runtime_checkable
class NotificationFormatter(Protocol):
    """Protocol for status notifications."""

    async def send_help(self, message: Any) -> None:
        """Send help message to user."""
        ...

    async def send_welcome(self, message: Any) -> None:
        """Send welcome message to user."""
        ...

    async def send_url_accepted_notification(
        self, message: Any, norm: str, correlation_id: str, *, silent: bool = False
    ) -> None:
        """Send URL accepted notification."""
        ...

    async def send_firecrawl_start_notification(
        self, message: Any, url: str | None = None, *, silent: bool = False
    ) -> None:
        """Send Firecrawl start notification."""
        ...

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
        ...

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
        ...

    async def send_cached_summary_notification(self, message: Any, *, silent: bool = False) -> None:
        """Inform the user that a cached summary is being reused."""
        ...

    async def send_html_fallback_notification(
        self, message: Any, content_len: int, *, silent: bool = False
    ) -> None:
        """Send HTML fallback notification."""
        ...

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
        ...

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
        ...

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
        ...

    async def send_llm_completion_notification(
        self, message: Any, llm: Any, correlation_id: str, *, silent: bool = False
    ) -> None:
        """Send LLM completion notification."""
        ...

    async def send_forward_accepted_notification(self, message: Any, title: str) -> None:
        """Send forward request accepted notification."""
        ...

    async def send_forward_language_notification(self, message: Any, detected: str | None) -> None:
        """Send forward language detection notification."""
        ...

    async def send_forward_completion_notification(self, message: Any, llm: Any) -> None:
        """Send forward completion notification."""
        ...

    async def send_youtube_download_notification(
        self, message: Any, url: str, *, silent: bool = False
    ) -> None:
        """Notify user that YouTube video download is starting."""
        ...

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
        ...

    async def send_error_notification(
        self,
        message: Any,
        error_type: str,
        correlation_id: str,
        details: str | None = None,
        reply_markup: Any | None = None,
    ) -> None:
        """Send error notification with rich formatting."""
        ...


@runtime_checkable
class SummaryPresenter(Protocol):
    """Protocol for summary presentation."""

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
        ...

    async def send_forward_summary_response(
        self,
        message: Any,
        forward_shaped: dict[str, Any],
        summary_id: int | str | None = None,
    ) -> None:
        """Send forward summary with per-field messages."""
        ...

    async def send_russian_translation(
        self, message: Any, translated_text: str, correlation_id: str | None = None
    ) -> None:
        """Send the adapted Russian translation as a follow-up message."""
        ...

    async def send_additional_insights_message(
        self, message: Any, insights: dict[str, Any], correlation_id: str | None = None
    ) -> None:
        """Send follow-up message summarizing additional research insights."""
        ...

    async def send_custom_article(self, message: Any, article: dict[str, Any]) -> None:
        """Send the custom generated article with a nice header and downloadable JSON."""
        ...

    async def send_related_reads(
        self,
        message: Any,
        items: list[RelatedReadItem],
        *,
        lang: str | None = None,
    ) -> None:
        """Send related-read follow-up suggestions."""
        ...


@runtime_checkable
class DatabasePresenter(Protocol):
    """Protocol for database-related UI presentation."""

    async def send_db_overview(self, message: Any, overview: dict[str, object]) -> None:
        """Send an overview of the database state."""
        ...

    async def send_db_verification(self, message: Any, verification: dict[str, Any]) -> None:
        """Send database verification summary highlighting missing fields."""
        ...

    async def send_db_reprocess_start(
        self,
        message: Any,
        *,
        url_targets: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
    ) -> None:
        """Notify the user that reprocessing of missing posts has started."""
        ...

    async def send_db_reprocess_complete(
        self,
        message: Any,
        *,
        url_targets: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
    ) -> None:
        """Summarize the outcome of the automated reprocessing."""
        ...

    async def send_topic_search_results(
        self,
        message: Any,
        *,
        topic: str,
        articles: Sequence[TopicArticle],
        source: str = "online",
    ) -> None:
        """Send a formatted list of topic search results to the user."""
        ...


@runtime_checkable
class ResponseFormatterFacade(
    DataFormatter,
    MessageValidator,
    ResponseSender,
    TextProcessor,
    NotificationFormatter,
    SummaryPresenter,
    DatabasePresenter,
    Protocol,
):
    """Compatibility protocol matching the legacy formatter facade surface."""

    MAX_BATCH_URLS: int
    MIN_MESSAGE_INTERVAL_MS: int
    progress_tracker: Any

    async def is_reader_mode(self, message: Any) -> bool:
        """Return whether the user prefers reader-mode progress updates."""
        ...

    def set_topic_manager(self, topic_manager: Any | None) -> None:
        """Update topic routing without rebuilding the formatter."""
        ...

    def set_reply_callbacks(
        self,
        *,
        safe_reply_func: Any = ...,
        reply_json_func: Any = ...,
    ) -> None:
        """Rebind runtime reply callbacks after construction."""
        ...
