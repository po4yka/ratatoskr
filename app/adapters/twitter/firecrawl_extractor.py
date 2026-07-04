"""Firecrawl tier for Twitter platform extraction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.content.article_media import extract_firecrawl_image_assets

if TYPE_CHECKING:
    import asyncio

    from app.adapters.content.scraper.protocol import ContentScraperProtocol
from app.adapters.content.quality_filters import detect_low_value_content
from app.adapters.twitter.article_quality import is_low_quality_article_content
from app.core.async_utils import raise_if_cancelled
from app.core.call_status import CallStatus
from app.core.html_utils import clean_markdown_article_text, html_to_text
from app.core.logging_utils import get_logger
from app.observability.failure_observability import (
    REASON_FIRECRAWL_ERROR,
    REASON_FIRECRAWL_LOW_VALUE,
    persist_request_failure,
)
from app.observability.metrics import record_twitter_article_extraction

logger = get_logger(__name__)


class TwitterFirecrawlExtractor:
    """Execute Firecrawl extraction and quality/persistence rules for Twitter."""

    def __init__(
        self,
        *,
        firecrawl: ContentScraperProtocol,
        firecrawl_sem: Any,
        schedule_crawl_persistence: Any,
        request_repo: Any,
    ) -> None:
        self._firecrawl = firecrawl
        self._firecrawl_sem = firecrawl_sem
        self._schedule_crawl_persistence = schedule_crawl_persistence
        self._request_repo = request_repo
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def extract(
        self,
        *,
        url_text: str,
        req_id: int | None,
        tweet_id: str | None,
        metadata: dict[str, Any],
        correlation_id: str | None,
        is_article: bool,
        persist_result: bool,
    ) -> tuple[bool, str, str]:
        try:
            async with self._firecrawl_sem():
                crawl = await self._firecrawl.scrape_markdown(url_text, request_id=req_id)

            if persist_result and req_id is not None:
                persist_task = self._schedule_crawl_persistence(req_id, crawl, correlation_id)
                if persist_task is not None:
                    self._background_tasks.add(persist_task)
                    persist_task.add_done_callback(self._background_tasks.discard)

            quality_issue = detect_low_value_content(crawl)
            if quality_issue and self.can_accept_low_value_firecrawl_content(
                quality_issue, is_article
            ):
                logger.info(
                    "twitter_firecrawl_accept_short_content",
                    extra={
                        "cid": correlation_id,
                        "tweet_id": tweet_id,
                        "quality_reason": quality_issue.get("reason"),
                    },
                )
                quality_issue = None
            if quality_issue and is_article:
                metadata["article_firecrawl_quality_reason"] = quality_issue.get("reason")
                logger.info(
                    "twitter_article_firecrawl_quality_fail",
                    extra={
                        "cid": correlation_id,
                        "reason": quality_issue.get("reason"),
                        "metrics": quality_issue.get("metrics"),
                    },
                )

            has_content = bool(
                crawl.status == CallStatus.OK
                and not quality_issue
                and (
                    (crawl.content_markdown and crawl.content_markdown.strip())
                    or (crawl.content_html and crawl.content_html.strip())
                )
            )

            if has_content:
                if crawl.content_markdown and crawl.content_markdown.strip():
                    content_text = clean_markdown_article_text(crawl.content_markdown)
                    content_source = "markdown"
                elif crawl.content_html and crawl.content_html.strip():
                    content_text = html_to_text(crawl.content_html)
                    content_source = "html"
                else:
                    return False, "", "none"

                if is_article and is_low_quality_article_content(content_text):
                    metadata["article_firecrawl_quality_reason"] = "ui_or_login"
                    logger.info(
                        "twitter_article_firecrawl_quality_fail",
                        extra={"cid": correlation_id, "reason": "ui_or_login"},
                    )
                    record_twitter_article_extraction(
                        stage="firecrawl",
                        status="failed",
                        reason="ui_or_login",
                    )
                    if req_id is not None:
                        await persist_request_failure(
                            request_repo=self._request_repo,
                            logger=logger,
                            request_id=req_id,
                            correlation_id=correlation_id,
                            stage="extraction",
                            component="twitter_firecrawl",
                            reason_code=REASON_FIRECRAWL_LOW_VALUE,
                            error=ValueError(
                                "Firecrawl article extraction produced UI/login content"
                            ),
                            retryable=True,
                            source_url=url_text,
                            quality_reason="ui_or_login",
                        )
                    return False, "", "none"

                metadata["extraction_method"] = "firecrawl"
                media_assets, media_selection = extract_firecrawl_image_assets(crawl)
                if media_selection["candidate_count"] > 0:
                    metadata["media_selection"] = media_selection
                if media_assets:
                    metadata["article_images"] = [asset.url for asset in media_assets if asset.url]
                if is_article:
                    metadata["article_extraction_stage"] = "firecrawl"
                logger.info(
                    "twitter_firecrawl_success",
                    extra={
                        "cid": correlation_id,
                        "content_len": len(content_text),
                        "tweet_id": tweet_id,
                    },
                )
                if is_article:
                    logger.info(
                        "twitter_article_extraction_success",
                        extra={
                            "cid": correlation_id,
                            "stage": "firecrawl",
                            "content_len": len(content_text),
                            "article_id": metadata.get("article_id"),
                        },
                    )
                    record_twitter_article_extraction(
                        stage="firecrawl",
                        status="success",
                        reason="ok",
                    )
                return True, content_text, content_source
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "twitter_firecrawl_failed",
                extra={"cid": correlation_id, "error": str(exc), "tweet_id": tweet_id},
            )
            if is_article:
                record_twitter_article_extraction(
                    stage="firecrawl",
                    status="failed",
                    reason="exception",
                )
                if req_id is not None:
                    await persist_request_failure(
                        request_repo=self._request_repo,
                        logger=logger,
                        request_id=req_id,
                        correlation_id=correlation_id,
                        stage="extraction",
                        component="twitter_firecrawl",
                        reason_code=REASON_FIRECRAWL_ERROR,
                        error=exc,
                        retryable=True,
                        source_url=url_text,
                    )
        return False, "", "none"

    @staticmethod
    def can_accept_low_value_firecrawl_content(
        quality_issue: dict[str, Any],
        is_article: bool,
    ) -> bool:
        if is_article:
            return False
        reason = str(quality_issue.get("reason") or "")
        return reason in {"content_too_short", "content_low_variation"}
