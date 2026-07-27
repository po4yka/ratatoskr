"""Restore missing reader source content through the established extraction path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.core.logging_utils import get_logger, redact_url_for_logging

logger = get_logger(__name__)


class SummaryContextReader(Protocol):
    async def get_summary_context_for_user(
        self,
        user_id: int,
        summary_id: int,
    ) -> dict[str, Any] | None: ...


class SourceContentExtractor(Protocol):
    async def extract_content_pure(
        self,
        url: str,
        correlation_id: str | None = None,
        request_id: int | None = None,
        *,
        update_request_on_failure: bool = True,
    ) -> tuple[str, str, dict[str, Any]]: ...


class RequestContentWriter(Protocol):
    async def async_update_request_content_text(
        self,
        request_id: int,
        content_text: str,
    ) -> None: ...


class SourceContentBackfillNotFoundError(Exception):
    """The summary does not exist or is not owned by the current user."""


class SourceContentBackfillUnavailableError(Exception):
    """The source cannot be re-extracted under the current record/configuration."""


class SourceContentBackfillFailedError(Exception):
    """The scraper chain could not restore usable source content."""


@dataclass(frozen=True, slots=True)
class SourceContentBackfillResult:
    summary_id: int
    request_id: int
    reextracted: bool
    content_source: str
    content_length: int


class SourceContentBackfillService:
    """Idempotently restore a summary's source body without re-running its LLM."""

    def __init__(
        self,
        *,
        summary_reader: SummaryContextReader,
        source_extractor: SourceContentExtractor,
        request_writer: RequestContentWriter,
        persist_source_content: bool,
    ) -> None:
        self._summary_reader = summary_reader
        self._source_extractor = source_extractor
        self._request_writer = request_writer
        self._persist_source_content = persist_source_content

    async def backfill(
        self,
        *,
        user_id: int,
        summary_id: int,
        operation_correlation_id: str | None = None,
    ) -> SourceContentBackfillResult:
        context = await self._summary_reader.get_summary_context_for_user(user_id, summary_id)
        if not context:
            raise SourceContentBackfillNotFoundError(summary_id)

        request_data = context.get("request") or {}
        request_id = int(context.get("request_id") or request_data.get("id") or 0)
        if request_id <= 0:
            raise SourceContentBackfillUnavailableError("Source request is missing")

        existing = _existing_content(context)
        if existing is not None:
            content_source, content_value = existing
            return SourceContentBackfillResult(
                summary_id=summary_id,
                request_id=request_id,
                reextracted=False,
                content_source=content_source,
                content_length=len(content_value),
            )

        if not self._persist_source_content:
            raise SourceContentBackfillUnavailableError(
                "Source retention is disabled for this deployment"
            )

        source_url = str(
            request_data.get("input_url") or request_data.get("normalized_url") or ""
        ).strip()
        if not source_url:
            raise SourceContentBackfillUnavailableError("Source URL is missing")

        source_correlation_id = (
            str(request_data.get("correlation_id") or "").strip() or operation_correlation_id
        )
        try:
            (
                content_text,
                content_source,
                _metadata,
            ) = await self._source_extractor.extract_content_pure(
                source_url,
                correlation_id=source_correlation_id,
                request_id=request_id,
                update_request_on_failure=False,
            )
        except Exception as exc:
            logger.warning(
                "source_content_backfill_extraction_failed",
                extra={
                    "summary_id": summary_id,
                    "request_id": request_id,
                    "cid": source_correlation_id,
                    "operation_correlation_id": operation_correlation_id,
                    "url": redact_url_for_logging(source_url),
                    "error_type": type(exc).__name__,
                },
            )
            raise SourceContentBackfillFailedError(summary_id) from exc

        content_text = content_text.strip()
        if not content_text:
            raise SourceContentBackfillFailedError(summary_id)

        await self._request_writer.async_update_request_content_text(request_id, content_text)
        logger.info(
            "source_content_backfill_completed",
            extra={
                "summary_id": summary_id,
                "request_id": request_id,
                "cid": source_correlation_id,
                "operation_correlation_id": operation_correlation_id,
                "content_source": content_source,
                "content_length": len(content_text),
            },
        )
        return SourceContentBackfillResult(
            summary_id=summary_id,
            request_id=request_id,
            reextracted=True,
            content_source=content_source,
            content_length=len(content_text),
        )


def _existing_content(context: dict[str, Any]) -> tuple[str, str] | None:
    crawl_result = context.get("crawl_result") or {}
    request_data = context.get("request") or {}
    transcription_artifact = context.get("transcription_artifact") or {}
    request_urls = {
        str(value).strip()
        for value in (request_data.get("input_url"), request_data.get("normalized_url"))
        if value
    }
    candidates = (
        ("text", crawl_result.get("content_text")),
        ("markdown", crawl_result.get("content_markdown")),
        ("html", crawl_result.get("content_html")),
        ("text", request_data.get("content_text")),
        ("transcript", transcription_artifact.get("plain_text")),
    )
    for content_source, value in candidates:
        if (
            isinstance(value, str)
            and value.strip()
            and not (content_source == "text" and value.strip() in request_urls)
        ):
            return content_source, value
    return None
