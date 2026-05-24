"""URL processing orchestration facade."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from app.adapters.content.cached_summary_responder import CachedSummaryResponder
from app.adapters.content.content_chunker import ContentChunker
from app.adapters.content.content_extractor import ContentExtractor
from app.adapters.content.interactive_summary_service import InteractiveSummaryService
from app.adapters.content.pure_summary_service import PureSummaryService
from app.adapters.content.streaming import StreamEvent, get_stream_hub
from app.adapters.content.summarization_models import (
    InteractiveSummaryRequest,
    InteractiveSummaryResult,
)
from app.adapters.content.summarization_runtime import SummarizationRuntime
from app.adapters.content.summary_request_factory import SummaryRequestFactory
from app.adapters.content.url_flow_context_builder import URLFlowContextBuilder
from app.adapters.content.url_flow_models import (
    URLFlowContext,
    URLFlowRequest,
    URLProcessingFlowResult,
    create_chunk_llm_stub,
)
from app.adapters.content.url_post_summary_task_service import URLPostSummaryTaskService
from app.adapters.content.url_summary_delivery_service import URLSummaryDeliveryService
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger, redact_url_for_logging

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.content.platform_extraction import PlatformExtractionRouter
    from app.adapters.content.scraper.protocol import ContentScraperProtocol
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
    from app.application.services.topic_search import TopicSearchService
    from app.config import AppConfig
    from app.db.session import Database
    from app.db.write_queue import DbWriteQueue

logger = get_logger(__name__)

__all__ = [
    "URLFlowContext",
    "URLFlowRequest",
    "URLProcessingFlowResult",
    "URLProcessor",
]


class URLProcessor:
    """Single composition root for the URL processing pipeline.

    Assembles ContentExtractor, SummarizationRuntime, SummaryDelivery, and
    PostSummaryTasks into a single ordered pipeline. Exists so that call sites
    (message_router, CLI) deal with one object instead of constructing and
    sequencing five services themselves.
    """

    def __init__(
        self,
        cfg: AppConfig,
        db: Database,
        firecrawl: ContentScraperProtocol,
        openrouter: LLMClientProtocol,
        response_formatter: ResponseFormatter,
        audit_func: Callable[[str, str, dict[str, Any]], None],
        sem: Callable[[], Any],
        topic_search: TopicSearchService | None = None,
        db_write_queue: DbWriteQueue | None = None,
        request_repo: RequestRepositoryPort | None = None,
        summary_repo: SummaryRepositoryPort | None = None,
        crawl_result_repo: CrawlResultRepositoryPort | None = None,
        llm_repo: LLMRepositoryPort | None = None,
        user_repo: UserRepositoryPort | None = None,
        related_reads_service: RelatedReadsService | None = None,
        stream_coordinator_factory: Callable[..., Any] | None = None,
        platform_router: PlatformExtractionRouter | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.response_formatter = response_formatter
        self._audit = audit_func
        self._db_write_queue = db_write_queue
        if request_repo is None:
            msg = "request_repo must be provided by the DI layer"
            raise ValueError(msg)
        if summary_repo is None:
            msg = "summary_repo must be provided by the DI layer"
            raise ValueError(msg)
        if crawl_result_repo is None:
            msg = "crawl_result_repo must be provided by the DI layer"
            raise ValueError(msg)
        if llm_repo is None:
            msg = "llm_repo must be provided by the DI layer"
            raise ValueError(msg)
        if user_repo is None:
            msg = "user_repo must be provided by the DI layer"
            raise ValueError(msg)
        self.request_repo = request_repo
        self.summary_repo = summary_repo

        self.content_extractor = ContentExtractor(
            cfg=cfg,
            db=db,
            firecrawl=firecrawl,
            response_formatter=response_formatter,
            audit_func=audit_func,
            sem=sem,
            quality_llm_client=openrouter,
            platform_router=platform_router,
        )
        self.content_chunker = ContentChunker(
            cfg=cfg,
            openrouter=openrouter,
            response_formatter=response_formatter,
            audit_func=audit_func,
            sem=sem,
        )
        self.summarization_runtime = SummarizationRuntime(
            cfg=cfg,
            db=db,
            openrouter=openrouter,
            response_formatter=response_formatter,
            audit_func=audit_func,
            sem=sem,
            topic_search=topic_search,
            db_write_queue=db_write_queue,
            summary_repo=summary_repo,
            request_repo=request_repo,
            crawl_result_repo=crawl_result_repo,
            llm_repo=llm_repo,
            user_repo=user_repo,
        )
        self.pure_summary_service = PureSummaryService(runtime=self.summarization_runtime)
        self.summary_request_factory = SummaryRequestFactory(
            runtime=self.summarization_runtime,
            select_max_tokens=self.pure_summary_service.select_max_tokens,
            stream_coordinator_factory=stream_coordinator_factory,
        )
        self.interactive_summary_service = InteractiveSummaryService(
            runtime=self.summarization_runtime,
            request_factory=self.summary_request_factory,
            pure_summary_service=self.pure_summary_service,
        )

        self.cached_summary_responder = CachedSummaryResponder(
            cfg=cfg,
            db=db,
            response_formatter=response_formatter,
            request_repo=self.request_repo,
            summary_repo=self.summary_repo,
        )
        self.context_builder = URLFlowContextBuilder(
            cfg=cfg,
            content_extractor=self.content_extractor,
            content_chunker=self.content_chunker,
            response_formatter=response_formatter,
        )
        self.summary_delivery = URLSummaryDeliveryService(
            cfg=cfg,
            db=db,
            response_formatter=response_formatter,
            summary_repo=self.summary_repo,
            audit_func=audit_func,
            request_repo=self.request_repo,
        )
        self.post_summary_tasks = URLPostSummaryTaskService(
            response_formatter=response_formatter,
            summary_repo=self.summary_repo,
            article_generator=self.summarization_runtime.article_generator,
            insights_generator=self.summarization_runtime.insights_generator,
            summary_delivery=self.summary_delivery,
            related_reads_service=related_reads_service,
        )

    @property
    def audit_func(self) -> Callable[[str, str, dict[str, Any]], None]:
        """Expose the audit callable to platform extractors that need to log events."""
        return self._audit

    async def aclose(self, timeout: float = 5.0) -> None:
        """Drain runtime and follow-up tasks before shutdown."""
        await self.summarization_runtime.aclose(timeout=timeout)
        await self.summary_delivery.aclose(timeout=timeout)
        await self.post_summary_tasks.aclose(timeout=timeout)

    async def handle_url_flow(
        self,
        request: URLFlowRequest,
    ) -> URLProcessingFlowResult:
        """Handle complete URL processing flow from extraction to follow-up tasks."""
        cached_result = await self.cached_summary_responder.maybe_reply(
            request.message,
            request.url_text,
            correlation_id=request.correlation_id,
            interaction_id=request.interaction_id,
            silent=request.effective_silent,
        )
        if cached_result is not None:
            return cached_result

        from app.utils.typing_indicator import typing_indicator

        async with typing_indicator(self.response_formatter, request.message):
            return await self._run_url_flow(request)

    async def _run_url_flow(
        self,
        request: URLFlowRequest,
    ) -> URLProcessingFlowResult:
        """Execute the URL processing pipeline (extraction -> summarization -> delivery)."""
        from app.observability.otel import get_tracer

        _tracer = get_tracer(__name__)
        with _tracer.start_as_current_span(
            "url_flow.process",
            attributes={
                "url": str(redact_url_for_logging(request.url_text)),
                "ratatoskr.correlation_id": request.correlation_id,
            },
        ):
            return await self._run_url_flow_inner(request)

    async def _run_url_flow_inner(
        self,
        request: URLFlowRequest,
    ) -> URLProcessingFlowResult:
        """Inner URL processing pipeline, wrapped by _run_url_flow span."""
        context: URLFlowContext | None = None
        started_at = time.monotonic()
        terminal_status: str = "succeeded"
        terminal_error_code: str | None = None
        terminal_error_message: str | None = None
        try:
            context = await self.context_builder.build(request)
            await self._record_url_flow_start(request=request, req_id=context.req_id)

            # Resolve the model that will actually be used (routing-aware)
            display_model = self.cfg.openrouter.model
            routing_cfg = self.cfg.model_routing
            if routing_cfg.enabled:
                from app.core.content_classifier import classify_content
                from app.core.model_router import resolve_model_for_content

                tier = classify_content(context.content_text, url=request.url_text)
                display_model = resolve_model_for_content(
                    tier=tier,
                    content_length=len(context.content_text),
                    has_images=bool(context.images),
                    routing_config=routing_cfg,
                    openrouter_config=self.cfg.openrouter,
                )

            if request.on_phase_change:
                await request.on_phase_change(
                    "analyzing",
                    context.title,
                    len(context.content_text),
                    display_model,
                )

            if request.on_phase_change:
                await request.on_phase_change(
                    "summarizing",
                    context.title,
                    len(context.content_text),
                    display_model,
                )

            if getattr(self.cfg.runtime, "url_flow_streaming_enabled", True):
                get_stream_hub().publish(
                    str(context.req_id),
                    StreamEvent.now(
                        "stage", {"stage": "summarizing"}, request.correlation_id or ""
                    ),
                )

            if context.should_chunk and context.chunks:
                summary_json = await self.content_chunker.process_chunks(
                    context.chunks,
                    context.system_prompt,
                    context.chosen_lang,
                    context.req_id,
                    request.correlation_id,
                )
                if summary_json:
                    summary_json = (
                        await self.summarization_runtime.semantic_helper.enrich_with_rag_fields(
                            summary_json,
                            content_text=context.content_text,
                            chosen_lang=context.chosen_lang,
                            req_id=context.req_id,
                        )
                    )
                    from app.core.summary_contract_impl.quality_metadata import (
                        merge_summary_quality_metadata,
                    )

                    merge_summary_quality_metadata(
                        summary_json,
                        structured_output_mode=getattr(
                            self.cfg.openrouter, "structured_output_mode", None
                        ),
                        model_used=getattr(self.cfg.openrouter, "model", None),
                        source_coverage=context.source_coverage,
                    )
                summary_result: InteractiveSummaryResult | None = InteractiveSummaryResult(
                    summary=summary_json,
                    llm_result=create_chunk_llm_stub(self.cfg) if summary_json else None,
                    served_from_cache=False,
                    model_used=getattr(self.cfg.openrouter, "model", None),
                )
            else:
                summary_result = await self.interactive_summary_service.summarize(
                    InteractiveSummaryRequest(
                        message=request.message,
                        content_text=context.content_text,
                        chosen_lang=context.chosen_lang,
                        system_prompt=context.system_prompt,
                        req_id=context.req_id,
                        max_chars=context.max_chars,
                        correlation_id=request.correlation_id,
                        interaction_id=request.interaction_id,
                        url_hash=context.dedupe_hash,
                        url=request.url_text,
                        silent=request.effective_silent,
                        on_phase_change=request.on_phase_change,
                        images=context.images,
                        progress_tracker=request.progress_tracker,
                        source_coverage=context.source_coverage,
                        extraction_quality=context.extraction_quality,
                        extraction_confidence=context.extraction_confidence,
                    )
                )

            summary_json = summary_result.summary if summary_result else None
            if summary_json is None:
                return await self.summary_delivery.send_processing_failure(
                    message=request.message,
                    url_text=request.url_text,
                    correlation_id=request.correlation_id,
                    silent=request.silent,
                    batch_mode=request.batch_mode,
                )

            _streaming_enabled = getattr(self.cfg.runtime, "url_flow_streaming_enabled", True)
            if _streaming_enabled:
                get_stream_hub().publish(
                    str(context.req_id),
                    StreamEvent.now("stage", {"stage": "validating"}, request.correlation_id or ""),
                )
                get_stream_hub().publish(
                    str(context.req_id),
                    StreamEvent.now("stage", {"stage": "persisting"}, request.correlation_id or ""),
                )

            result = await self.summary_delivery.deliver_summary(
                message=request.message,
                summary_result=summary_result,
                context=context,
                correlation_id=request.correlation_id,
                interaction_id=request.interaction_id,
                silent=request.silent,
                batch_mode=request.batch_mode,
            )

            if _streaming_enabled:
                get_stream_hub().publish(
                    str(context.req_id),
                    StreamEvent.now("stage", {"stage": "done"}, request.correlation_id or ""),
                )

            if not request.batch_mode:
                await self.post_summary_tasks.schedule_tasks(
                    request.message,
                    context.content_text,
                    context.chosen_lang,
                    context.req_id,
                    request.correlation_id,
                    summary_json,
                    needs_ru_translation=context.needs_ru_translation,
                    silent=request.silent,
                    url_hash=context.dedupe_hash,
                )

            return result
        except Exception as exc:
            raise_if_cancelled(exc)
            import traceback as _tb

            _error_class = type(exc).__name__
            _error_message = str(exc) or "<empty>"
            _top_frame = ""
            if exc.__traceback__ is not None:
                _frames = _tb.extract_tb(exc.__traceback__)
                if _frames:
                    _last = _frames[-1]
                    _top_frame = f"{_last.filename}:{_last.lineno}"
            _compact_extra = {
                "cid": request.correlation_id,
                "url": redact_url_for_logging(request.url_text),
                "error_class": _error_class,
                "error_message": _error_message,
                "top_frame": _top_frame,
            }
            if logger.isEnabledFor(10):  # logging.DEBUG
                logger.exception("url_processing_failed", extra=_compact_extra)
            else:
                logger.warning("url_processing_failed", extra=_compact_extra)
            terminal_status = "failed"
            terminal_error_code = _error_class
            terminal_error_message = _error_message
            # Safety-net: ensure request is marked as failed in DB
            req_id = context.req_id if context is not None else None
            if req_id is not None:
                try:
                    from app.domain.models.request import RequestStatus

                    await self.request_repo.async_update_request_status(req_id, RequestStatus.ERROR)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "failed_to_update_request_status_on_error",
                        extra={"cid": request.correlation_id, "req_id": req_id},
                    )
            if not request.silent and not request.batch_mode:
                # Render a paper-specific paywall diagnostic when the
                # academic adapter signals 'neither abstract nor PDF
                # reachable' — the generic 'AI models returned data
                # that couldn't be parsed' template is misleading for
                # papers that never reached an LLM.
                from app.adapters.academic.platform_extractor import (
                    AcademicPaperUnavailableError,
                )

                if isinstance(exc, AcademicPaperUnavailableError):
                    paper_details = (
                        f"{exc.host.upper()} paper unavailable "
                        f"({exc.reason}). Neither the abstract nor the PDF "
                        f"could be reached — this paper is likely behind a "
                        f"login or paywall, or the host's anti-bot is "
                        f"blocking this request."
                    )
                    await self.response_formatter.send_error_notification(
                        request.message,
                        "processing_failed",
                        request.correlation_id or "unknown",
                        details=paper_details,
                    )
                else:
                    await self.response_formatter.send_error_notification(
                        request.message,
                        "processing_failed",
                        request.correlation_id or "unknown",
                    )
            return URLProcessingFlowResult(success=False)
        finally:
            elapsed_seconds = max(0.0, time.monotonic() - started_at)
            await self._record_url_flow_terminal(
                request=request,
                req_id=context.req_id if context is not None else None,
                elapsed_seconds=elapsed_seconds,
                status=terminal_status,
                error_code=terminal_error_code,
                error_message=terminal_error_message,
            )

    async def _record_url_flow_start(
        self,
        *,
        request: URLFlowRequest,
        req_id: int,
    ) -> None:
        """Write a `running` job row at URL flow entry for crash-recovery.

        If the bot process dies mid-flow (OOM, signal, container restart) the
        worker's reconcile_stuck_processing_requests will reap rows whose lease
        expired without a terminal status and re-queue the request. Fails closed
        (logged + swallowed) so observability bugs cannot break URL handling.
        """
        try:
            from app.api.background.durable_jobs import RequestProcessingJobRepository

            repo = RequestProcessingJobRepository(self.db)
            await repo.record_synchronous_start(
                request_id=req_id,
                correlation_id=request.correlation_id,
                lease_ttl_seconds=int(getattr(self.cfg.runtime, "url_flow_lease_ttl_sec", 900)),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "url_flow_job_start_record_failed",
                extra={
                    "cid": request.correlation_id,
                    "req_id": req_id,
                    "error": str(exc),
                },
            )

    async def _record_url_flow_terminal(
        self,
        *,
        request: URLFlowRequest,
        req_id: int | None,
        elapsed_seconds: float,
        status: str,
        error_code: str | None,
        error_message: str | None,
    ) -> None:
        """Emit per-request wall-time metric and record terminal job row.

        Run in the ``_run_url_flow_inner`` ``finally`` so we capture both the
        happy path and exceptions. Bot-originated URL requests do not pass
        through the worker queue (no enqueue at submit time), so the job row
        here is the only persistence marker for synchronous runs. Metric +
        DB write each fail closed (logged + swallowed) so observability
        bugs cannot break user-visible URL handling.
        """
        slow_threshold = float(getattr(self.cfg.runtime, "llm_request_slow_threshold_sec", 300.0))
        try:
            from app.observability.metrics import record_llm_request_total_latency

            record_llm_request_total_latency(
                request_type="url",
                total_latency_seconds=elapsed_seconds,
                slow_threshold_seconds=slow_threshold,
            )
        except Exception as exc:
            logger.warning(
                "url_flow_metric_emit_failed",
                extra={"cid": request.correlation_id, "error": str(exc)},
            )
        # Log a slow-request warning so the signal is visible without a
        # metrics scrape. Threshold matches the histogram counter so log
        # lines and metric increments are paired.
        if elapsed_seconds >= slow_threshold:
            logger.warning(
                "url_flow_slow_request",
                extra={
                    "cid": request.correlation_id,
                    "req_id": req_id,
                    "elapsed_seconds": round(elapsed_seconds, 1),
                    "status": status,
                    "url": redact_url_for_logging(request.url_text),
                },
            )

        if req_id is None:
            return
        try:
            from app.api.background.durable_jobs import RequestProcessingJobRepository

            repo = RequestProcessingJobRepository(self.db)
            await repo.record_synchronous_outcome(
                request_id=req_id,
                correlation_id=request.correlation_id,
                status=status,
                error_code=error_code,
                error_message=error_message,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "url_flow_job_outcome_record_failed",
                extra={
                    "cid": request.correlation_id,
                    "req_id": req_id,
                    "error": str(exc),
                },
            )
