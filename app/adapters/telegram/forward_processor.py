"""Refactored forward processor using modular components."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine, Mapping
from typing import TYPE_CHECKING, Any

from app.adapters.content.summarization_runtime import SummarizationRuntime
from app.adapters.telegram.forward_content_processor import ForwardContentProcessor
from app.adapters.telegram.forward_summarizer import ForwardSummarizer
from app.application.services.user_interaction_service import async_safe_update_user_interaction
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.domain.models.request import RequestStatus

if TYPE_CHECKING:
    from app.adapters.content.content_extractor import ContentExtractor
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.llm.protocol import LLMClientProtocol
    from app.application.ports.requests import (
        CrawlResultRepositoryPort,
        LLMRepositoryPort,
        RequestRepositoryPort,
    )
    from app.application.ports.summaries import SummaryRepositoryPort
    from app.application.ports.users import UserRepositoryPort
    from app.application.services.related_reads_service import RelatedReadsService
    from app.config import AppConfig
    from app.db.session import Database
    from app.db.write_queue import DbWriteQueue

logger = get_logger(__name__)

# Background tasks (article generation, insights) are killed after this timeout
# to prevent hung LLM calls from accumulating indefinitely.
_BACKGROUND_TASK_TIMEOUT_SEC = 300


class ForwardProcessor:
    """Refactored forward processor using modular components."""

    def __init__(
        self,
        cfg: AppConfig,
        db: Database,
        openrouter: LLMClientProtocol,
        response_formatter: ResponseFormatter,
        audit_func: Callable[[str, str, dict], None],
        sem: Callable[[], Any],
        db_write_queue: DbWriteQueue | None = None,
        *,
        summary_repo: SummaryRepositoryPort,
        request_repo: RequestRepositoryPort,
        crawl_result_repo: CrawlResultRepositoryPort,
        llm_repo: LLMRepositoryPort,
        user_repo: UserRepositoryPort,
        related_reads_service: RelatedReadsService | None = None,
        content_extractor: ContentExtractor | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.summary_repo = summary_repo
        self.request_repo = request_repo
        self.crawl_result_repo = crawl_result_repo
        self.llm_repo = llm_repo
        self.user_repo = user_repo
        self.response_formatter = response_formatter
        self._audit = audit_func
        self._sem = sem
        self._db_write_queue = db_write_queue
        self._related_reads_service = related_reads_service
        self._summarization_runtime: SummarizationRuntime | None = None
        # Strong references to in-flight fire-and-forget tasks (insights,
        # related-reads). Without this set the event loop only holds a weak
        # reference and may GC a task mid-run; drained on shutdown via aclose().
        self._background_tasks: set[asyncio.Task[Any]] = set()

        # Enrich forwarded-post summaries with the content of embedded links,
        # when a content extractor is available to scrape them.
        link_enricher = None
        if content_extractor is not None:
            from app.adapters.content.forward_link_enricher import ForwardLinkEnricher

            link_enricher = ForwardLinkEnricher(cfg=cfg, content_extractor=content_extractor)

        # Initialize components
        self.content_processor = ForwardContentProcessor(
            cfg=cfg,
            db=db,
            response_formatter=response_formatter,
            audit_func=audit_func,
            forward_link_enricher=link_enricher,
        )

        self.summarizer = ForwardSummarizer(
            cfg=cfg,
            db=db,
            openrouter=openrouter,
            response_formatter=response_formatter,
            audit_func=audit_func,
            sem=sem,
            db_write_queue=db_write_queue,
            summary_repo=summary_repo,
            request_repo=request_repo,
            llm_repo=llm_repo,
            user_repo=user_repo,
        )

    async def handle_forward_flow(
        self, message: Any, *, correlation_id: str | None = None, interaction_id: int | None = None
    ) -> None:
        """Handle complete forwarded message processing flow."""
        try:
            # Process forward content
            (
                req_id,
                prompt,
                chosen_lang,
                system_prompt,
            ) = await self.content_processor.process_forward_content(message, correlation_id)

            if await self._maybe_reply_with_cached_summary(
                message,
                req_id,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
            ):
                return

            # Summarize content
            forward_shaped = await self.summarizer.summarize_forward(
                message, prompt, chosen_lang, system_prompt, req_id, correlation_id, interaction_id
            )

            if forward_shaped:
                # Send formatted preview for forward flow with action buttons
                await self.response_formatter.send_forward_summary_response(
                    message,
                    forward_shaped,
                    summary_id=f"req:{req_id}" if req_id else None,
                )

                summary_payload: dict[str, Any] | None = (
                    dict(forward_shaped) if isinstance(forward_shaped, dict) else None
                )

                # NOTE: standalone-article generation is intentionally NOT
                # scheduled here. The forward summary card already carries
                # TL;DR, tags, entities and categories, so a follow-up
                # "standalone article from topics & tags" duplicates the
                # delivered content. The background LLM call could also
                # stall (e.g. structured-output 422 on qwen-flash), leaving
                # the user with a dangling "Crafting…" notice. The URL flow
                # still has its own custom-article generation in
                # url_post_summary_task_service.py.

                self._schedule_background_task(
                    self._run_forward_insights(
                        message,
                        chosen_lang,
                        req_id,
                        correlation_id,
                        summary_payload,
                    ),
                    correlation_id,
                    "additional_insights_forward",
                )

                if self._related_reads_service is not None and summary_payload:
                    self._schedule_background_task(
                        self._send_related_reads(
                            message, summary_payload, req_id, correlation_id, chosen_lang
                        ),
                        correlation_id,
                        "related_reads_forward",
                    )

        except Exception as e:
            logger.exception("forward_flow_error", extra={"error": str(e), "cid": correlation_id})
            try:
                await self.response_formatter.send_error_notification(
                    message,
                    "processing_failed",
                    correlation_id or "unknown",
                )
            except Exception:
                logger.debug(
                    "forward_flow_error_notification_failed", extra={"cid": correlation_id}
                )

    def _schedule_background_task(
        self, coro: Coroutine[Any, Any, Any], correlation_id: str | None, label: str
    ) -> asyncio.Task[Any] | None:
        async def _with_timeout() -> Any:
            try:
                async with asyncio.timeout(_BACKGROUND_TASK_TIMEOUT_SEC):
                    return await coro
            except TimeoutError:
                logger.warning(
                    "background_task_timeout",
                    extra={
                        "cid": correlation_id,
                        "label": label,
                        "timeout_sec": _BACKGROUND_TASK_TIMEOUT_SEC,
                    },
                )
                return None

        try:
            task: asyncio.Task[Any] = asyncio.create_task(_with_timeout())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except RuntimeError as exc:
            logger.error(
                "background_task_schedule_failed",
                extra={"cid": correlation_id, "label": label, "error": str(exc)},
            )
            return None

        def _log_task_error(t: asyncio.Task) -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.warning(
                    "background_task_failed",
                    extra={"cid": correlation_id, "label": label, "error": str(exc)},
                )

        task.add_done_callback(_log_task_error)
        return task

    async def aclose(self, timeout: float = 5.0) -> None:
        """Drain in-flight forward-flow background tasks on shutdown."""
        if not self._background_tasks:
            return
        tasks = list(self._background_tasks)
        try:
            async with asyncio.timeout(timeout):
                await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError:
            logger.warning(
                "forward_processor_shutdown_timeout",
                extra={"pending": len(self._background_tasks)},
            )
        except Exception as e:
            raise_if_cancelled(e)
            logger.error("forward_processor_shutdown_error", extra={"error": str(e)})

    async def _send_related_reads(
        self,
        message: Any,
        summary_payload: dict[str, Any],
        request_id: int,
        correlation_id: str | None,
        lang: str,
    ) -> None:
        try:
            if self._related_reads_service is None:
                return
            items = await self._related_reads_service.find_related(
                summary_payload, exclude_request_id=request_id
            )
            if items:
                await self.response_formatter.send_related_reads(
                    message,
                    items,
                    lang=lang,
                )
        except Exception as exc:
            logger.warning(
                "related_reads_forward_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )

    async def _run_forward_insights(
        self,
        message: Any,
        chosen_lang: str,
        req_id: int,
        correlation_id: str | None,
        summary_payload: dict[str, Any] | None,
    ) -> None:
        content_text = await self._get_forward_content_text(message, req_id)
        if not content_text:
            return
        await self._handle_additional_insights(
            message,
            content_text,
            chosen_lang,
            req_id,
            correlation_id,
            summary=summary_payload,
        )

    async def _maybe_reply_with_cached_summary(
        self,
        message: Any,
        req_id: int,
        *,
        correlation_id: str | None,
        interaction_id: int | None,
    ) -> bool:
        """Return True if a cached summary exists for the forward request."""
        summary_row = await self.summary_repo.async_get_summary_by_request(req_id)
        if not summary_row:
            return False

        payload = summary_row.get("json_payload")
        if not payload:
            return False

        try:
            shaped = json.loads(payload)
        except json.JSONDecodeError:
            return False

        await self.response_formatter.send_cached_summary_notification(message)
        await self.response_formatter.send_forward_summary_response(
            message,
            shaped,
            summary_id=f"req:{req_id}" if req_id else None,
        )

        await self.request_repo.async_update_request_status(req_id, RequestStatus.COMPLETED)

        if interaction_id:
            await async_safe_update_user_interaction(
                self.user_repo,
                interaction_id=interaction_id,
                response_sent=True,
                response_type="summary",
                request_id=req_id,
                logger_=logger,
            )

        self._audit(
            "INFO",
            "forward_summary_cache_hit",
            {"request_id": req_id, "cid": correlation_id},
        )
        return True

    async def _get_forward_content_text(self, message: Any, req_id: int) -> str | None:
        """Extract the content text for a forward message from the requests table."""
        try:
            request_row = await self.request_repo.async_get_request_by_id(req_id)
            if not request_row:
                return None

            content_text = request_row.get("content_text")
            if isinstance(content_text, str):
                return content_text

            return None
        except Exception as exc:
            logger.exception(
                "get_forward_content_text_failed",
                extra={"error": str(exc), "req_id": req_id},
            )
            return None

    def _get_summarization_runtime(self) -> SummarizationRuntime:
        """Lazily create shared summarization dependencies for background tasks."""
        if self._summarization_runtime is None:
            self._summarization_runtime = SummarizationRuntime(
                cfg=self.cfg,
                db=self.db,
                openrouter=self.summarizer.openrouter,
                response_formatter=self.response_formatter,
                audit_func=self._audit,
                sem=self._sem,
                db_write_queue=self._db_write_queue,
                summary_repo=self.summary_repo,
                request_repo=self.request_repo,
                crawl_result_repo=self.crawl_result_repo,
                llm_repo=self.llm_repo,
                user_repo=self.user_repo,
            )
        return self._summarization_runtime

    async def _handle_additional_insights(
        self,
        message: Any,
        content_text: str,
        chosen_lang: str,
        req_id: int,
        correlation_id: str | None,
        summary: dict[str, Any] | None = None,
    ) -> None:
        """Generate and persist additional insights using the LLM."""
        logger.info(
            "insights_flow_started_for_forward",
            extra={"cid": correlation_id, "content_len": len(content_text), "lang": chosen_lang},
        )

        try:
            summary_payload: dict[str, Any] | None = None
            if isinstance(summary, Mapping):
                summary_payload = dict(summary)
            if summary_payload is None:
                try:
                    row = await self.summary_repo.async_get_summary_by_request(req_id)
                    json_payload = row.get("json_payload") if row else None
                    if json_payload:
                        summary_payload = json.loads(json_payload)
                except Exception as exc:
                    logger.debug(
                        "forward_insights_summary_load_failed",
                        extra={"cid": correlation_id, "error": str(exc)},
                    )

            insights_generator = self._get_summarization_runtime().insights_generator

            insights = await insights_generator.generate_additional_insights(
                message,
                content_text=content_text,
                chosen_lang=chosen_lang,
                req_id=req_id,
                correlation_id=correlation_id,
                summary=summary_payload,
            )

            if insights:
                logger.info(
                    "insights_generated_successfully_for_forward",
                    extra={
                        "cid": correlation_id,
                        "facts_count": len(insights.get("new_facts", [])),
                        "has_overview": bool(insights.get("topic_overview")),
                    },
                )

                await self.response_formatter.send_additional_insights_message(
                    message, insights, correlation_id
                )

                logger.info("insights_message_sent_for_forward", extra={"cid": correlation_id})

                try:
                    await self.summary_repo.async_update_summary_insights(req_id, insights)
                    logger.debug(
                        "insights_persisted_for_forward",
                        extra={"cid": correlation_id, "request_id": req_id},
                    )
                except Exception as exc:
                    logger.exception(
                        "persist_insights_error_for_forward",
                        extra={"cid": correlation_id, "error": str(exc)},
                    )
            else:
                logger.warning(
                    "insights_generation_returned_empty_for_forward",
                    extra={"cid": correlation_id, "reason": "LLM returned None or empty insights"},
                )

        except Exception as exc:
            logger.exception(
                "insights_flow_error_for_forward",
                extra={"cid": correlation_id, "error": str(exc)},
            )
