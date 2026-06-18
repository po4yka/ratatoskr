"""Request, crawl, LLM, and video-download ports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, TypedDict, runtime_checkable

from app.domain.models.request import RequestStatus

if TYPE_CHECKING:
    from datetime import datetime


class LLMCallRecord(TypedDict, total=False):
    """Typed record for persisting an LLM call."""

    request_id: int | None
    provider: str | None
    model: str | None
    endpoint: str | None
    request_headers_json: Any
    request_messages_json: Any
    response_text: str | None
    response_json: Any
    tokens_prompt: int | None
    tokens_completion: int | None
    cost_usd: float | None
    latency_ms: int | None
    status: str | None
    error_text: str | None
    structured_output_used: bool | None
    structured_output_mode: str | None
    error_context_json: Any
    # Attempt-tracking fields (improvement #6).
    # attempt_index: when omitted the repository computes max(attempt_index)+1
    # for the same request_id within the same transaction.
    attempt_index: int | None
    attempt_trigger: str | None


@runtime_checkable
class RequestRepositoryPort(Protocol):
    """Port for request read operations used in application use cases."""

    async def async_get_request_id_by_url_with_summary(self, user_id: int, url: str) -> int | None:
        """Return request ID for URL owned by user that has a summary."""

    async def async_get_request_by_id(self, request_id: int) -> dict[str, Any] | None:
        """Return request by ID."""

    async def async_get_request_by_telegram_message(
        self,
        *,
        user_id: int,
        message_id: int,
    ) -> dict[str, Any] | None:
        """Return a user's request matched by bot reply or input Telegram message ID."""

    async def async_get_request_context(self, request_id: int) -> dict[str, Any] | None:
        """Return request joined with its crawl result and summary."""

    async def async_get_request_by_dedupe_hash(self, dedupe_hash: str) -> dict[str, Any] | None:
        """Return request by dedupe hash."""

    async def async_get_request_by_paper_canonical_id(
        self, paper_canonical_id: str
    ) -> dict[str, Any] | None:
        """Return request by canonical academic-paper id (e.g. ``arxiv:2301.00001``)."""

    async def async_get_latest_request_by_correlation_id(
        self, correlation_id: str
    ) -> dict[str, Any] | None:
        """Return the most recent request matching correlation_id."""

    async def async_get_requests_by_ids(
        self, request_ids: list[int], user_id: int | None = None
    ) -> dict[int, dict[str, Any]]:
        """Return requests mapped by ID."""

    async def async_create_request(
        self,
        *,
        type_: str = "url",
        status: RequestStatus = RequestStatus.PENDING,
        correlation_id: str | None = None,
        chat_id: int | None = None,
        user_id: int | None = None,
        input_url: str | None = None,
        normalized_url: str | None = None,
        dedupe_hash: str | None = None,
        paper_canonical_id: str | None = None,
        input_message_id: int | None = None,
        fwd_from_chat_id: int | None = None,
        fwd_from_msg_id: int | None = None,
        lang_detected: str | None = None,
        content_text: str | None = None,
        route_version: int = 1,
        initial_attempt_trigger: str | None = None,
    ) -> int:
        """Create a request."""

    async def async_create_request_once(
        self,
        *,
        type_: str = "url",
        status: RequestStatus = RequestStatus.PENDING,
        correlation_id: str | None = None,
        chat_id: int | None = None,
        user_id: int | None = None,
        input_url: str | None = None,
        normalized_url: str | None = None,
        dedupe_hash: str | None = None,
        paper_canonical_id: str | None = None,
        input_message_id: int | None = None,
        fwd_from_chat_id: int | None = None,
        fwd_from_msg_id: int | None = None,
        lang_detected: str | None = None,
        content_text: str | None = None,
        route_version: int = 1,
        initial_attempt_trigger: str | None = None,
    ) -> tuple[int, bool]:
        """Create a request atomically, returning whether the row was inserted."""

    async def async_create_minimal_request(
        self,
        *,
        type_: str = "url",
        status: RequestStatus = RequestStatus.PENDING,
        correlation_id: str | None = None,
        chat_id: int | None = None,
        user_id: int | None = None,
        input_url: str | None = None,
        normalized_url: str | None = None,
        dedupe_hash: str | None = None,
    ) -> tuple[int, bool]:
        """Create a minimal request row."""

    async def async_get_request_by_forward(
        self, chat_id: int, fwd_message_id: int
    ) -> dict[str, Any] | None:
        """Return request by forward source identifiers."""

    async def async_update_request_status(self, request_id: int, status: str) -> None:
        """Update request status."""

    async def async_update_request_status_with_correlation(
        self,
        request_id: int,
        status: str,
        correlation_id: str | None,
    ) -> None:
        """Update request status and correlation ID."""

    async def async_update_request_lang_detected(self, request_id: int, lang: str) -> None:
        """Update detected language."""

    async def async_update_request_correlation_id(
        self,
        request_id: int,
        correlation_id: str,
    ) -> None:
        """Update correlation ID."""

    async def async_update_request_content_text(
        self,
        request_id: int,
        content_text: str,
    ) -> None:
        """Update the extracted/requested content text."""

    async def async_update_request_error(
        self,
        request_id: int,
        status: str,
        error_type: str | None = None,
        error_message: str | None = None,
        processing_time_ms: int | None = None,
        error_context_json: Any | None = None,
    ) -> None:
        """Persist structured request error details."""

    async def async_get_request_error_context(self, request_id: int) -> dict[str, Any] | None:
        """Return structured request error context."""

    async def async_count_pending_requests_before(self, created_at: datetime) -> int:
        """Count pending requests created before the supplied timestamp."""

    async def async_get_all_for_user(self, user_id: int) -> list[dict[str, Any]]:
        """Return all request rows for sync operations."""

    async def async_get_max_server_version(self, user_id: int) -> int | None:
        """Return the maximum server_version for requests owned by *user_id*."""

    async def async_update_bot_reply_message_id(
        self, request_id: int, bot_reply_message_id: int
    ) -> None:
        """Persist the Telegram message-id of the bot's reply for a request."""

    async def async_insert_telegram_message(
        self,
        *,
        request_id: int,
        message_id: int | None,
        chat_id: int | None,
        date_ts: int | None,
        text_full: str | None,
        entities_json: Any,
        media_type: str | None,
        media_file_ids_json: Any,
        forward_from_chat_id: int | None,
        forward_from_chat_type: str | None,
        forward_from_chat_title: str | None,
        forward_from_message_id: int | None,
        forward_date_ts: int | None,
        telegram_raw_json: Any,
    ) -> int:
        """Persist a Telegram message snapshot and return its row id."""


@runtime_checkable
class CrawlResultRepositoryPort(Protocol):
    """Port for crawl-result query operations."""

    async def async_insert_crawl_result(
        self,
        request_id: int,
        success: bool,
        markdown: str | None = None,
        html: str | None = None,
        error: str | None = None,
        metadata_json: dict[str, Any] | None = None,
        *,
        source_url: str | None = None,
        http_status: int | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        latency_ms: int | None = None,
        correlation_id: str | None = None,
        options_json: dict[str, Any] | None = None,
        attempt_log: list[dict[str, Any]] | None = None,
        winning_provider: str | None = None,
    ) -> int:
        """Insert a crawl result and return the row id."""

    async def async_get_crawl_result_by_request(self, request_id: int) -> dict[str, Any] | None:
        """Return crawl result by request ID."""

    async def async_get_all_for_user(self, user_id: int) -> list[dict[str, Any]]:
        """Return all crawl rows for sync operations."""

    async def async_get_max_server_version(self, user_id: int) -> int | None:
        """Return the maximum server_version for crawl results owned by *user_id*."""


@runtime_checkable
class LLMRepositoryPort(Protocol):
    """Port for LLM-call query operations."""

    async def async_get_llm_calls_by_request(self, request_id: int) -> list[dict[str, Any]]:
        """Return LLM calls by request ID."""

    async def async_count_llm_calls_by_request(self, request_id: int) -> int:
        """Return the number of LLM calls by request ID."""

    async def async_insert_llm_call(self, record: LLMCallRecord) -> int:
        """Persist an LLM call."""

    async def async_insert_llm_calls_batch(self, calls: list[dict[str, Any]]) -> list[int]:
        """Persist a batch of LLM calls."""

    async def async_get_latest_llm_model_by_request_id(self, request_id: int) -> str | None:
        """Return the latest model used for a request."""

    async def async_get_all_for_user(self, user_id: int) -> list[dict[str, Any]]:
        """Return all LLM rows for sync operations."""

    async def async_get_max_server_version(self, user_id: int) -> int | None:
        """Return the maximum server_version for LLM calls owned by *user_id*."""

    async def async_get_latest_error_by_request(self, request_id: int) -> dict[str, Any] | None:
        """Return the latest error-like LLM call for a request."""

    async def async_get_cost_usd_since(self, since: datetime) -> float:
        """Return summed LLM cost since the supplied timestamp."""


@runtime_checkable
class VideoDownloadRepositoryPort(Protocol):
    async def async_get_video_download_by_request(
        self,
        request_id: int,
    ) -> dict[str, Any] | None:
        """Return video-download record by request ID."""

    async def async_create_video_download(
        self,
        request_id: int,
        video_id: str,
        status: str = "pending",
    ) -> int:
        """Create a video-download row."""

    async def async_update_video_download(self, download_id: int, **kwargs: Any) -> None:
        """Update a video-download row."""

    async def async_update_video_download_status(
        self,
        download_id: int,
        status: str,
        error_text: str | None = None,
        download_started_at: Any | None = None,
    ) -> None:
        """Update video-download status."""
