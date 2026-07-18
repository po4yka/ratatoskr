"""Graph-backed URL-flow facade (T9 cutover seam, ADR-0013).

``GraphURLProcessor`` exposes the SAME public contract as the legacy
:class:`~app.adapters.content.url_processor.URLProcessor` so re-pointing the
production callers is a DI swap, not a rewrite:

- :meth:`handle_url_flow` -- drop-in for ``URLProcessor.handle_url_flow``: the
  full interactive/silent/batch URL flow (cache short-circuit, typing indicator,
  OTel span, in-flight gauge + latency metric, request-row creation, the
  RequestProcessingJob crash-recovery lease, graph invocation, terminal-failure
  notification, post-summary tasks).
- :meth:`summarize` -- content-only drop-in for the 4
  ``PureSummaryService.summarize`` callers (api/background/handlers, rss): the
  caller pre-extracts and passes ``content_text``; the graph skips extraction and
  runs the summarize portion, returning the shaped summary dict + request quality
  metadata.

This is an ADAPTER (it orchestrates concrete adapters + the graph runner); it
delegates extraction+ground+summarize+validate+repair+enrich+persist+notify to
the summarize graph (``run_summarize_graph`` / ``run_summarize_graph_streamed``)
and does NOT extract or summarize itself. Post-T9 it is the ONLY URL-flow path:
every production caller is wired to this facade via the DI layer (no flag gate).
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

# Route version for the URL graph path -- mirrors the legacy ``URL_ROUTE_VERSION``
# the lifecycle stamps on ``requests.route_version`` so graph-path rows are
# indistinguishable from the legacy path (no code model default; a plain int).
from app.adapters.content.content_extractor import URL_ROUTE_VERSION
from app.adapters.content.url_flow_models import (
    URLFlowContext,
    URLFlowRequest,
    URLProcessingFlowResult,
    create_chunk_llm_stub,
)
from app.application.graphs.summarize.graph import (
    DEFAULT_RECURSION_LIMIT,
    build_initial_state,
    cleanup_checkpoint_thread,
    invocation_config,
)
from app.core.async_utils import raise_if_cancelled
from app.core.lang import LANG_RU, choose_language, detect_language
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.core.summary_contract_impl.quality_metadata import merge_summary_quality_metadata
from app.core.url_utils import compute_dedupe_hash
from app.core.validation import (
    safe_message_id,
    safe_telegram_chat_id,
    safe_telegram_user_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.content.cached_summary_responder import CachedSummaryResponder
    from app.adapters.content.summarization_models import PureSummaryRequest
    from app.adapters.content.url_post_summary_task_service import URLPostSummaryTaskService
    from app.adapters.content.url_summary_delivery_service import URLSummaryDeliveryService
    from app.application.graphs.summarize.deps import SummarizeDeps
    from app.application.ports.message_persistence import MessagePersistencePort
    from app.application.ports.requests import RequestRepositoryPort
    from app.application.ports.stream_sink import StreamSinkPort
    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)

__all__ = ["GraphURLProcessor"]


class GraphURLProcessor:
    """Drive the summarize graph with the legacy ``URLProcessor`` orchestration.

    Same public surface as ``URLProcessor`` (``handle_url_flow`` + ``summarize``)
    so re-pointing callers is a DI swap. Owns the legacy non-graph orchestration
    concerns (cache short-circuit, span, gauge, latency metric, request row, the
    synchronous crash-recovery lease, terminal notification, post-summary tasks)
    while delegating extraction->notify to the graph runner.
    """

    def __init__(
        self,
        *,
        cfg: AppConfig,
        db: Database,
        graph: Any,
        deps: SummarizeDeps,
        stream_sink_factory: Callable[[], StreamSinkPort],
        streamed_runner: Callable[..., Any],
        cached_summary_responder: CachedSummaryResponder,
        post_summary_tasks: URLPostSummaryTaskService,
        summary_delivery: URLSummaryDeliveryService,
        response_formatter: Any,
        request_repo: RequestRepositoryPort,
        message_persistence: MessagePersistencePort,
        content_extractor: Any | None = None,
        summary_repo: Any | None = None,
        audit_func: Any | None = None,
        summarization_runtime: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self._graph = graph
        self._deps = deps
        self._stream_sink_factory = stream_sink_factory
        # The streamed runner (``run_summarize_graph_streamed``) is injected as
        # a callable so this adapter never imports the composition tier. The plain
        # ``run_summarize_graph`` is in the application layer and imported directly.
        self._streamed_runner = streamed_runner
        self.cached_summary_responder = cached_summary_responder
        self.post_summary_tasks = post_summary_tasks
        self.summary_delivery = summary_delivery
        self.response_formatter = response_formatter
        self.request_repo = request_repo
        # The persistence facade owns request creation AND the telegram_messages
        # snapshot + User/Chat upserts (persist-everything). The graph-path
        # request row MUST mirror the legacy lifecycle: create + snapshot in one
        # seam so every row carries its owner ``user_id`` (IDOR rule 12) and a
        # ``telegram_messages`` row exists.
        self.message_persistence = message_persistence
        # Reach-through collaborators the legacy ``URLProcessor`` object exposed
        # (NOT a summarize path -- ``handle_url_flow`` / ``summarize`` always run the
        # graph). The DI layer re-exposes the already-wired ``ContentExtractor`` +
        # ``summary_repo`` + ``audit_func`` here so the extraction/persistence
        # consumers (forward, aggregation, MCP, url_handler, api background extract)
        # keep working when DI returns the facade in place of the legacy object.
        self.content_extractor = content_extractor
        self.summary_repo = summary_repo
        # Re-exposed (like summary_repo) so reach-through consumers such as the
        # batch relationship/combined-summary flow can persist their own
        # llm_calls rows (rule 3) without threading DI bundles.
        self.llm_repo = getattr(deps, "llm_repo", None)
        self.audit_func = audit_func
        # Shared follow-up runtime (article/insights generators feed
        # ``post_summary_tasks``). Retained only so the bot shutdown drain reaches
        # it -- NOT a summarize path. Tolerates ``None`` (content-only/test wiring).
        self.summarization_runtime = summarization_runtime

    async def aclose(self, timeout: float = 5.0) -> None:
        """Drain follow-up tasks before shutdown (parity with the legacy aclose)."""
        if self.summarization_runtime is not None:
            await self.summarization_runtime.aclose(timeout=timeout)
        await self.summary_delivery.aclose(timeout=timeout)
        await self.post_summary_tasks.aclose(timeout=timeout)

    # ------------------------------------------------------------------ #
    # Public contract -- mirrors URLProcessor
    # ------------------------------------------------------------------ #
    async def handle_url_flow(self, request: URLFlowRequest) -> URLProcessingFlowResult:
        """Handle the complete URL flow via the summarize graph.

        Parity concern 1 -- DB-row cache short-circuit: ``maybe_reply`` first; a
        hit returns the cached result without invoking the graph.
        """
        cached_result = await self.cached_summary_responder.maybe_reply(
            request.message,
            request.url_text,
            correlation_id=request.correlation_id,
            interaction_id=request.interaction_id,
            silent=request.effective_silent,
        )
        if cached_result is not None:
            await self._react(request, "✅")  # cache hit is a success outcome
            return cached_result

        # Parity concern 2 -- typing indicator wrapping the active flow.
        from app.utils.typing_indicator import typing_indicator

        async with typing_indicator(self.response_formatter, request.message):
            await self._react(request, "👀")  # accepted / processing
            result = await self._run_url_flow(request)
        await self._react(request, "✅" if getattr(result, "success", False) else "❌")
        return result

    async def _react(self, request: URLFlowRequest, emoji: str) -> None:
        """Best-effort emoji ack on the user's URL message (zero chat clutter)."""
        if request.message is None:
            return
        chat_id, _user_id, message_id = _message_identity(request.message)
        if chat_id and message_id:
            await self.response_formatter.react(chat_id=chat_id, message_id=message_id, emoji=emoji)

    async def summarize(self, request: PureSummaryRequest) -> dict[str, Any]:
        """Content-only summarization for pre-extracted callers (handlers/rss).

        The graph extracts internally, but these callers pass pre-extracted
        ``content_text``. We build the initial state with ``source_text`` set and
        an empty ``input_url`` so the ``extract`` node no-ops (it returns ``{}``
        when no URL is settled) and the pre-provided content is summarized. Returns
        the shaped summary dict with the request quality metadata
        (source_coverage/extraction_quality/extraction_confidence) the legacy
        ``PureSummaryService.summarize`` applies -- the graph nodes do not.

        Empty/whitespace content raises ``ValueError`` byte-for-byte with the
        legacy ``PureSummaryService.summarize`` so the 4 callers
        (``api/background/handlers``, rss) see identical error semantics -- they
        wrap this in a ``StageError`` on failure and never branch on an empty-dict
        sentinel.

        A graph invocation FAILURE (a raised exception) is RE-RAISED, not swallowed
        to ``{}`` (audit #4): the background ``BackgroundRetryRunner.run_with_backoff``
        only retries a stage that *raises*, so returning ``{}`` on failure silently
        bypassed the configured ``retry_attempts``. ``{}`` is reserved for the genuine
        no-summary case (the graph completed but produced no summary) -- the caller's
        ``if not summary_json`` then raises a terminal ``StageError`` with no retry,
        which is the correct outcome for "ran fine, nothing to summarize".
        """
        content_text = request.content_text or ""
        if not content_text.strip():
            raise ValueError("Content text is empty or contains only whitespace")

        lang = request.chosen_lang or "en"
        # Resolve the fallback ONCE so the graph state's correlation_id and the
        # langgraph thread_id stay identical (sacred, ADR-0011 / Operating Rule 1).
        # Applying "content-only" only to invocation_config (as before) diverged the
        # two whenever request.correlation_id was falsy: state kept "" while the
        # checkpointer keyed on "content-only".
        correlation_id = request.correlation_id or "content-only"
        if request.request_id is not None:
            # API/background callers already own a real Request row. Route through
            # the full runner so summarize/repair attempts and terminal failures are
            # durably attached to that request instead of being discarded by the
            # historical request_id=None content-only shortcut.
            from app.application.graphs.summarize.graph import run_summarize_graph

            user_scope, environment = self._retrieval_scope()
            final_state = await run_summarize_graph(
                graph=self._graph,
                deps=self._deps,
                correlation_id=correlation_id,
                request_id=request.request_id,
                lang=lang,
                input_url="",
                source_text=content_text,
                user_scope=user_scope,
                environment=environment,
                two_pass_eligible=False,
            )
        else:
            initial_state = build_initial_state(
                correlation_id=correlation_id,
                request_id=None,
                lang=lang,
                input_url="",
                source_text=content_text,
            )
            config = invocation_config(
                correlation_id=correlation_id,
                recursion_limit=DEFAULT_RECURSION_LIMIT,
            )
            try:
                final_state = await self._graph.ainvoke(initial_state, config=config)
            except Exception as exc:
                raise_if_cancelled(exc)
                logger.warning(
                    "graph_content_only_summarize_failed",
                    extra={"cid": correlation_id, "error": str(exc)},
                )
                await cleanup_checkpoint_thread(self._graph, config)
                raise
            await cleanup_checkpoint_thread(self._graph, config)

        # Terminal graph failure: the graph's route_terminal_failure node populated
        # ``state['error']`` and exited without a summary. Re-raise as ValueError so
        # the background retry runner (run_with_backoff) sees a raised exception and
        # fires the configured retry_attempts -- returning {} here would have silently
        # bypassed retries (audit finding [1]: graph terminal failure returned as empty
        # dict, not re-raised).
        if isinstance(final_state, dict) and "error" in final_state:
            error_val = final_state["error"]
            raise ValueError(str(error_val))

        # ``{}`` is reserved for the genuine no-summary case: the graph completed
        # successfully but produced no summary. The caller's ``if not summary_json``
        # raises a terminal StageError (no retry) -- correct for "ran fine, nothing
        # to summarize" (audit #4).
        summary = final_state.get("summary") if isinstance(final_state, dict) else None
        if not isinstance(summary, dict) or not summary:
            return {}

        # audit #5: restore the two summary-completion steps the legacy
        # ``ensure_summary_payload`` ran but the content-only graph path lost --
        # LLM metadata-completion (title/author/dates) + RAG-field enrichment. Both
        # run via the port-safe app service (llm_client port + pure core helpers);
        # best-effort so a completion failure never blocks the summary.
        await self._complete_metadata_and_enrich(
            summary, content_text, lang, correlation_id, request.request_id
        )

        # Apply the request quality metadata the graph nodes do not set (parity
        # with PureSummaryService._apply_request_quality_metadata).
        merge_summary_quality_metadata(
            summary,
            source_coverage=request.source_coverage,
            extraction_quality=request.extraction_quality,
            extraction_confidence=request.extraction_confidence,
        )
        return summary

    async def create_text_request(
        self,
        *,
        message: Any,
        request_type: str,
        correlation_id: str | None,
        content_text: str | None = None,
    ) -> int:
        """Create a persisted request row for non-URL text sources such as voice transcripts."""
        chat_id, user_id, input_message_id = _message_identity_flexible(message)
        req_id = await self.message_persistence.request_repo.async_create_request(
            type_=request_type,
            correlation_id=correlation_id,
            chat_id=chat_id,
            user_id=user_id,
            input_message_id=input_message_id,
            content_text=content_text,
            route_version=URL_ROUTE_VERSION,
            initial_attempt_trigger="initial",
        )
        if message is not None:
            try:
                await self.message_persistence.persist_message_snapshot(req_id, message)
            except Exception as exc:
                raise_if_cancelled(exc)
                logger.error(
                    "graph_text_flow_snapshot_error",
                    extra={"cid": correlation_id, "req_id": req_id, "error": str(exc)},
                )
        return req_id

    async def summarize_text_request(
        self,
        *,
        message: Any,
        request_id: int,
        content_text: str,
        correlation_id: str | None,
        interaction_id: int | None = None,
        request_type: str = "text",
        silent: bool = False,
    ) -> URLProcessingFlowResult:
        """Summarize already-extracted text and persist it under ``request_id``."""
        if not content_text.strip():
            raise ValueError("Content text is empty or contains only whitespace")
        await self.request_repo.async_update_request_content_text(request_id, content_text)
        detected_lang = detect_language(content_text)
        try:
            await self.request_repo.async_update_request_lang_detected(request_id, detected_lang)
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "graph_text_flow_lang_persist_failed",
                extra={"cid": correlation_id, "req_id": request_id, "error": str(exc)},
            )
        lang = choose_language(getattr(self.cfg.runtime, "preferred_lang", None), detected_lang)
        user_scope, environment = self._retrieval_scope()

        # Route through run_summarize_graph -- NOT a bare self._graph.ainvoke. A bare
        # ainvoke skipped the single terminal-failure sink, so a node / repair-budget /
        # recursion failure lost BOTH the accumulated summarize+repair llm_calls
        # (persist-everything, rule 3) and the structured failure snapshot
        # (stage/component/reason_code/retryable), writing only a degraded
        # async_update_request_error status. run_summarize_graph catches the failure and
        # route_terminal_failure persists the recovered llm_calls + the proper snapshot
        # before returning {"error": ...}. ``source_text`` seeds the pre-extracted
        # transcript/document (empty input_url -> extract no-ops); two_pass_eligible is
        # False to keep enrichment scoped to the URL path (audit #20). An empty
        # correlation_id falls back to a synthetic per-request thread_id so the
        # checkpointer never collides distinct requests on "".
        from app.application.graphs.summarize.graph import run_summarize_graph

        final_state = await run_summarize_graph(
            graph=self._graph,
            deps=self._deps,
            correlation_id=correlation_id or f"{request_type}-{request_id}",
            request_id=request_id,
            lang=lang,
            input_url="",
            source_text=content_text,
            user_scope=user_scope,
            environment=environment,
            two_pass_eligible=False,
        )

        # Terminal graph failure: route_terminal_failure already marked the request
        # ERROR with the structured snapshot AND persisted the accumulated llm_calls.
        # Return failure (mirrors handle_url_flow); the caller sends the user notice.
        if isinstance(final_state, dict) and "error" in final_state:
            return URLProcessingFlowResult(success=False, request_id=request_id)
        summary_json = final_state.get("summary") if isinstance(final_state, dict) else None
        if not isinstance(summary_json, dict) or not summary_json:
            # Graph completed but produced no summary -- no exception was raised, so the
            # terminal sink did NOT run and the request is still 'processing'. Finalize
            # it explicitly (a genuine no-summary, not a graph failure).
            await self.request_repo.async_update_request_error(
                request_id,
                "error",
                error_type="empty_summary",
                error_message="No summary was produced",
            )
            return URLProcessingFlowResult(success=False, request_id=request_id)

        result = URLProcessingFlowResult.from_summary(
            summary_json,
            cached=False,
            request_id=request_id,
        )
        will_send_bilingual_ru = (
            not silent
            and LANG_RU not in (detected_lang, lang)
            and bool(getattr(self.cfg.runtime, "summary_bilingual_enabled", False))
        )
        if not silent:
            await self._deliver_text_summary(
                message=message,
                request_id=request_id,
                result=result,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
                request_type=request_type,
                suppress_tldr_ru=will_send_bilingual_ru,
            )
        await self.post_summary_tasks.schedule_tasks(
            message,
            content_text,
            lang,
            request_id,
            correlation_id,
            summary_json,
            # Non-Russian uploaded/text materials get the full bilingual treatment
            # too (gated downstream by SUMMARY_BILINGUAL_ENABLED + not silent).
            needs_ru_translation=(not silent and LANG_RU not in (detected_lang, lang)),
            silent=silent,
            url_hash=f"{request_type}:{request_id}",
        )
        return result

    async def _deliver_text_summary(
        self,
        *,
        message: Any,
        request_id: int,
        result: URLProcessingFlowResult,
        correlation_id: str | None,
        interaction_id: int | None,
        request_type: str,
        suppress_tldr_ru: bool = False,
    ) -> None:
        context = URLFlowContext(
            dedupe_hash=f"{request_type}:{request_id}",
            req_id=request_id,
            content_text="",
            title=result.title,
            images=None,
            chosen_lang=getattr(self.cfg.runtime, "preferred_lang", None) or "auto",
            needs_ru_translation=False,
            system_prompt="",
            should_chunk=False,
            max_chars=0,
            chunks=None,
        )
        summary_result = _SummaryResultStub(
            summary=_summary_for_delivery(result.summary_json, suppress_tldr_ru=suppress_tldr_ru),
            llm_result=create_chunk_llm_stub(self.cfg),
            served_from_cache=False,
            model_used=getattr(self.cfg.openrouter, "model", None),
        )
        await self.summary_delivery.deliver_summary(
            message=message,
            summary_result=summary_result,
            context=context,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            silent=False,
            batch_mode=False,
        )

    async def _complete_metadata_and_enrich(
        self,
        summary: dict[str, Any],
        content_text: str,
        lang: str,
        correlation_id: str,
        request_id: int | None = None,
    ) -> None:
        """LLM metadata-completion + RAG-field enrichment for the content-only path.

        Ports legacy ``ensure_summary_payload``'s two completion steps (audit #5)
        the content-only graph path lost. Port-safe: the LLM call goes through the
        ``llm_client`` port; the RAG enrichment is a pure ``app.core`` computation.

        When ``request_id`` is provided and ``self._deps.llm_repo`` is available the
        metadata-completion LLM call record is persisted (persist-everything, audit
        finding [1]).  When ``request_id`` is None (the standard content-only path
        where the caller owns the request row) persistence is skipped with a debug
        log -- FK-violating against ``requests.id=None`` would raise
        ForeignKeyViolationError.
        """
        from app.application.services.summarization.rag_enrichment import (
            LLM_METADATA_FIELDS,
            complete_summary_metadata_via_llm,
            enrich_summary_rag_fields,
        )

        # Step 1: LLM metadata-completion for blank title/author/published_at/last_updated.
        metadata = summary.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            summary["metadata"] = metadata
        missing = [
            f
            for f in LLM_METADATA_FIELDS
            if not (isinstance(metadata.get(f), str) and metadata[f].strip())
        ]
        if missing and content_text.strip():
            llm_client = getattr(self._deps, "llm_client", None)
            if llm_client is not None:
                try:
                    completed, record = await complete_summary_metadata_via_llm(
                        llm_client=llm_client,
                        content_text=content_text,
                        fields=missing,
                        request_id=request_id,
                        correlation_id=correlation_id,
                        structured_output_mode=getattr(
                            self.cfg.openrouter, "structured_output_mode", None
                        ),
                    )
                    for key, value in completed.items():
                        if value and key in missing:
                            metadata[key] = value
                    # Persist the LLM call record when a request row is available
                    # (persist-everything).  When request_id is None the caller owns
                    # the request row and handles persistence downstream; skip to avoid
                    # FK violation.
                    llm_repo = getattr(self._deps, "llm_repo", None)
                    if record is not None and request_id is not None and llm_repo is not None:
                        try:
                            await llm_repo.async_insert_llm_call(record)
                        except Exception as exc:
                            raise_if_cancelled(exc)
                            logger.warning(
                                "content_only_metadata_llm_call_persist_failed",
                                extra={
                                    "cid": correlation_id,
                                    "req_id": request_id,
                                    "error": str(exc),
                                },
                            )
                    elif record is not None and request_id is None:
                        logger.debug(
                            "content_only_metadata_llm_call_not_persisted",
                            extra={
                                "cid": correlation_id,
                                "reason": "no request_id on content-only path; caller owns persistence",
                            },
                        )
                except Exception as exc:
                    raise_if_cancelled(exc)
                    logger.warning(
                        "content_only_metadata_completion_failed",
                        extra={"cid": correlation_id, "error": str(exc)},
                    )

        # Step 2: pure RAG-field enrichment (semantic_boosters/keywords/chunks).
        try:
            await enrich_summary_rag_fields(
                summary,
                content_text=content_text,
                chosen_lang=lang,
                request_id=None,
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "content_only_rag_enrichment_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )

    # ------------------------------------------------------------------ #
    # Internal orchestration (mirrors URLProcessor._run_url_flow*)
    # ------------------------------------------------------------------ #
    async def _run_url_flow(self, request: URLFlowRequest) -> URLProcessingFlowResult:
        """Wrap the flow in the OTel ``url_flow.process`` span (parity concern 2)."""
        from app.observability.otel import get_tracer, set_correlation_id_attr

        _tracer = get_tracer(__name__)
        with _tracer.start_as_current_span(
            "url_flow.process",
            attributes={
                "url": str(redact_url_for_logging(request.url_text)),
                "ratatoskr.correlation_id": request.correlation_id,
            },
        ):
            set_correlation_id_attr(request.correlation_id)
            return await self._run_url_flow_inner(request)

    async def _run_url_flow_inner(self, request: URLFlowRequest) -> URLProcessingFlowResult:
        """Inner pipeline: request row + lease + graph invocation + delivery."""
        from app.observability.metrics import set_url_processor_in_flight

        req_id: int | None = None
        started_at = time.monotonic()
        terminal_status = "succeeded"
        terminal_error_code: str | None = None
        terminal_error_message: str | None = None

        # Parity concern 3 -- in-flight gauge.
        set_url_processor_in_flight(+1)
        try:
            # Parity concern 4 -- create the request row WITHOUT running extraction
            # (the graph's extract node extracts). dedupe_hash via url_utils.
            req_id = await self._create_request_row(request)

            # Parity concern 5 -- RequestProcessingJob crash-recovery lease.
            await self._record_url_flow_start(request=request, req_id=req_id)

            lang = self._resolve_lang(request)

            # Parity concern 6 -- run the graph. Interactive -> streamed runner with
            # a sink; silent/batch -> non-streamed.
            final_state = await self._run_graph(request=request, req_id=req_id, lang=lang)

            # Parity concern 7 -- map result -> URLProcessingFlowResult; terminal
            # error -> failure notification with Error ID.
            if isinstance(final_state, dict) and "error" in final_state:
                terminal_status = "failed"
                terminal_error_code = "graph_terminal_failure"
                terminal_error_message = str(final_state.get("error"))
                await self._notify_terminal_failure(request, final_state)
                return URLProcessingFlowResult(success=False, request_id=req_id)

            summary_json = final_state.get("summary") if isinstance(final_state, dict) else None
            if not isinstance(summary_json, dict) or not summary_json:
                # No summary produced (e.g. empty content) -> failure delivery.
                terminal_status = "failed"
                return await self.summary_delivery.send_processing_failure(
                    message=request.message,
                    url_text=request.url_text,
                    correlation_id=request.correlation_id,
                    silent=request.silent,
                    batch_mode=request.batch_mode,
                )

            result = URLProcessingFlowResult.from_summary(
                summary_json, cached=False, request_id=req_id
            )

            # The graph re-detects the content language during extract; read it back
            # from final_state so the chosen output lang + RU-translation gating match
            # the legacy context builder (which fed both off ``extraction.detected_lang``)
            # rather than the pre-graph preferred_lang. Computed BEFORE delivery so the
            # EN card knows whether a full Russian block will follow.
            detected_lang = self._detected_lang(final_state)
            chosen_lang = choose_language(
                getattr(self.cfg.runtime, "preferred_lang", None), detected_lang
            )
            will_send_bilingual_ru = (
                not request.batch_mode
                and self._needs_ru_translation(request, chosen_lang, detected_lang)
                and bool(getattr(self.cfg.runtime, "summary_bilingual_enabled", False))
            )

            # Parity concern 9 -- bot_reply_message_id update (delivery concern).
            await self._persist_bot_reply(
                request, req_id, result, suppress_tldr_ru=will_send_bilingual_ru
            )

            # Parity concern 8 -- post-summary follow-up tasks, gated like legacy
            # (only when not batch and the graph produced a summary). Fire-and-forget.
            if not request.batch_mode:
                await self.post_summary_tasks.schedule_tasks(
                    request.message,
                    final_state.get("source_text") or "",
                    chosen_lang,
                    req_id,
                    request.correlation_id,
                    summary_json,
                    needs_ru_translation=self._needs_ru_translation(
                        request, chosen_lang, detected_lang
                    ),
                    silent=request.silent,
                    url_hash=str(
                        final_state.get("dedupe_hash") or compute_dedupe_hash(request.url_text)
                    ),
                )

            return result
        except asyncio.CancelledError:
            terminal_status = "failed"
            terminal_error_code = "CancelledError"
            terminal_error_message = "URL flow cancelled before completion"
            raise
        except Exception as exc:
            raise_if_cancelled(exc)
            terminal_status = "failed"
            terminal_error_code = type(exc).__name__
            terminal_error_message = str(exc) or "<empty>"
            logger.warning(
                "graph_url_flow_failed",
                extra={
                    "cid": request.correlation_id,
                    "url": redact_url_for_logging(request.url_text),
                    "error_class": terminal_error_code,
                    "error_message": terminal_error_message,
                },
            )
            await self._mark_request_error(request, req_id)
            await self._notify_orchestration_failure(request, exc)
            return URLProcessingFlowResult(success=False, request_id=req_id)
        finally:
            # Parity concern 3 -- gauge release + total-latency metric + terminal
            # lease row (the ONLY crash-recovery marker for synchronous requests).
            set_url_processor_in_flight(-1)
            elapsed_seconds = max(0.0, time.monotonic() - started_at)
            await self._record_url_flow_terminal(
                request=request,
                req_id=req_id,
                elapsed_seconds=elapsed_seconds,
                status=terminal_status,
                error_code=terminal_error_code,
                error_message=terminal_error_message,
            )

    def _streaming_selected(self, request: URLFlowRequest) -> bool:
        """Whether to drive the graph via the streamed runner for this request.

        Streaming is reserved for interactive requests AND requires the
        ``SUMMARY_STREAMING_ENABLED`` runtime flag (default on). Silent/batch
        callers never stream. Honoring the flag here keeps it from being dead
        for the URL path (audit #19): with the flag off, even interactive URL
        summaries take the plain ``ainvoke`` runner.
        """
        if request.effective_silent:
            return False
        runtime = self.cfg.runtime
        if not bool(getattr(runtime, "summary_streaming_enabled", True)):
            return False
        if str(getattr(runtime, "summary_streaming_mode", "section")).lower() != "section":
            return False
        scope = str(getattr(runtime, "summary_streaming_provider_scope", "openrouter")).lower()
        if scope == "disabled":
            return False
        if scope == "all":
            return True
        provider = str(getattr(runtime, "llm_provider", "openrouter")).lower()
        return provider == scope

    async def _run_graph(
        self, *, request: URLFlowRequest, req_id: int, lang: str
    ) -> dict[str, Any]:
        """Invoke the streamed runner (interactive + flag on) or the plain runner."""
        user_scope, environment = self._retrieval_scope()
        if not self._streaming_selected(request):
            from app.application.graphs.summarize.graph import run_summarize_graph

            return await run_summarize_graph(
                graph=self._graph,
                deps=self._deps,
                correlation_id=request.correlation_id or "",
                request_id=req_id,
                lang=lang,
                input_url=request.url_text,
                user_scope=user_scope,
                environment=environment,
            )

        sink = self._stream_sink_factory()
        return await self._streamed_runner(
            graph=self._graph,
            deps=self._deps,
            sink=sink,
            correlation_id=request.correlation_id or "",
            request_id=req_id,
            lang=lang,
            input_url=request.url_text,
            user_scope=user_scope,
            environment=environment,
        )

    async def _create_request_row(self, request: URLFlowRequest) -> int:
        """Create/resolve the request row (idempotent on dedupe_hash) for the flow.

        Mirrors the legacy ``PlatformRequestLifecycle.create_request`` seam without
        running extraction: ``async_create_request_once`` resolves a repeat URL to
        its existing row and reports the dedupe hit atomically. A hit refreshes the
        correlation id for the active flow, while ``persist_message_snapshot`` writes
        the ``telegram_messages`` row + User/Chat upserts (persist-everything). The
        graph's extract node attaches its crawl results / failures to this request_id.

        ``chat_id`` / ``user_id`` / ``input_message_id`` are extracted at the
        Telegram boundary via ``safe_telegram_*`` exactly like the legacy lifecycle
        (``from_user.id`` / ``chat.id`` / message id) -- a NULL ``user_id`` here would
        break the defense-in-depth IDOR ownership filter (rule 12).
        """
        from app.core.url_utils import normalize_url

        if request.existing_request_id is not None:
            return request.existing_request_id

        dedupe_hash = compute_dedupe_hash(request.url_text)
        normalized = normalize_url(request.url_text)
        chat_id, user_id, input_message_id = _message_identity(request.message)
        req_id, created = await self.message_persistence.request_repo.async_create_request_once(
            type_="url",
            correlation_id=request.correlation_id,
            chat_id=chat_id,
            user_id=user_id,
            input_url=request.url_text,
            normalized_url=normalized,
            dedupe_hash=dedupe_hash,
            input_message_id=input_message_id,
            content_text=request.url_text,
            route_version=URL_ROUTE_VERSION,
            initial_attempt_trigger="initial",
        )
        if not created and request.correlation_id:
            await self.message_persistence.request_repo.async_update_request_correlation_id(
                req_id, request.correlation_id
            )
        # persist-everything: snapshot the originating Telegram message (and upsert
        # the sending User / Chat) so the graph path is row-for-row identical to the
        # legacy lifecycle. Best-effort, mirroring legacy: a snapshot failure must
        # not abort the flow (the request row already exists).
        if request.message is not None and request.persist_message_snapshot:
            try:
                await self.message_persistence.persist_message_snapshot(req_id, request.message)
            except Exception as exc:
                raise_if_cancelled(exc)
                logger.error(
                    "graph_url_flow_snapshot_error",
                    extra={"cid": request.correlation_id, "req_id": req_id, "error": str(exc)},
                )
        return req_id

    def _resolve_lang(self, request: URLFlowRequest) -> str:
        """Seed the graph's initial ``lang`` with the RAW runtime preference.

        Under the shipped ``preferred_lang: auto`` we must NOT collapse to ``en``
        here: the extract node re-resolves ``state['lang']`` via
        ``choose_language(preferred_lang, detected_lang)`` once the content's
        language is known, so a premature ``auto -> en`` collapse would force every
        non-English article onto the English summary/prompt/cache path before extract
        ever runs. A forced ``en``/``ru`` preference still pins the output downstream.
        Post-summary gating reads the detected language back from ``final_state`` (see
        ``_detected_lang`` / the post-summary block) so it matches the legacy
        context builder.
        """
        return getattr(self.cfg.runtime, "preferred_lang", None) or "auto"

    @staticmethod
    def _detected_lang(final_state: Any) -> str | None:
        """Read the content language the graph's extract node detected, if any."""
        if not isinstance(final_state, dict):
            return None
        detected = final_state.get("detected_lang")
        return detected if isinstance(detected, str) and detected else None

    def _needs_ru_translation(
        self, request: URLFlowRequest, chosen_lang: str, detected_lang: str | None = None
    ) -> bool:
        """Russian-translation gating (parity with the context builder).

        Mirrors ``url_flow_context_builder``: translate only for an interactive
        (non-silent) flow where NEITHER the detected source language NOR the chosen
        output language is already Russian.
        """
        return not request.silent and LANG_RU not in (detected_lang, chosen_lang)

    def _retrieval_scope(self) -> tuple[str | None, str | None]:
        """Owner-wide retrieval scope for the ground/persist nodes (IDOR partition).

        Sourced from ``cfg.vector_store`` -- the canonical owner partition the
        index writers + ground reads share (matches di/search.py, di/shared.py).
        """
        vector_store = getattr(self.cfg, "vector_store", None)
        if vector_store is None:
            return None, None
        return (
            getattr(vector_store, "user_scope", None),
            getattr(vector_store, "environment", None),
        )

    async def _persist_bot_reply(
        self,
        request: URLFlowRequest,
        req_id: int,
        result: URLProcessingFlowResult,
        *,
        suppress_tldr_ru: bool = False,
    ) -> None:
        """Deliver the summary + persist bot_reply_message_id via the delivery service.

        The delivery service owns both the structured-summary send and the
        bot_reply_message_id persistence (parity concern 9). We build the minimal
        context it needs from the graph result. When ``suppress_tldr_ru`` is set a
        full Russian block will follow, so the inline TL;DR (RU) is stripped from
        the primary card to avoid showing the Russian TL;DR twice.
        """
        if request.effective_silent:
            return
        context = self._context_for_delivery(request, req_id, result)
        from app.adapters.content.url_flow_models import create_chunk_llm_stub

        summary_result = _SummaryResultStub(
            summary=_summary_for_delivery(result.summary_json, suppress_tldr_ru=suppress_tldr_ru),
            llm_result=create_chunk_llm_stub(self.cfg),
            served_from_cache=False,
            model_used=getattr(self.cfg.openrouter, "model", None),
        )
        try:
            await self.summary_delivery.deliver_summary(
                message=request.message,
                summary_result=summary_result,
                context=context,
                correlation_id=request.correlation_id,
                interaction_id=request.interaction_id,
                silent=request.silent,
                batch_mode=request.batch_mode,
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "graph_url_flow_delivery_failed",
                extra={"cid": request.correlation_id, "req_id": req_id, "error": str(exc)},
            )
            await self._notify_delivery_failure(request)

    async def _notify_delivery_failure(self, request: URLFlowRequest) -> None:
        """Best-effort fallback notice when delivery fails after COMPLETED persist.

        The summary is already persisted (request stays ``COMPLETED``); without
        this the user gets neither the summary card nor any error, since
        ``deliver_summary`` already swallowed the exception. Points the user at
        ``/history`` instead of re-raising, so the ``COMPLETED`` semantics hold.
        Guarded so a failure here can never propagate.
        """
        if request.silent or request.batch_mode:
            return
        try:
            await self.response_formatter.send_error_notification(
                request.message,
                "unexpected_error",
                request.correlation_id or "unknown",
                details=(
                    "Your summary was generated and saved, but delivering it here "
                    "failed. Use /history to view it."
                ),
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "graph_url_flow_delivery_failure_notice_failed",
                extra={"cid": request.correlation_id, "error": str(exc)},
            )

    def _context_for_delivery(
        self, request: URLFlowRequest, req_id: int, result: URLProcessingFlowResult
    ) -> URLFlowContext:
        """Minimal URLFlowContext for the delivery service (delivery-only fields)."""
        lang = self._resolve_lang(request)
        return URLFlowContext(
            dedupe_hash=compute_dedupe_hash(request.url_text),
            req_id=req_id,
            content_text="",
            title=result.title,
            images=None,
            chosen_lang=lang,
            needs_ru_translation=self._needs_ru_translation(request, lang),
            system_prompt="",
            should_chunk=False,
            max_chars=0,
            chunks=None,
        )

    async def _notify_terminal_failure(
        self, request: URLFlowRequest, final_state: dict[str, Any]
    ) -> None:
        """Surface the graph's terminal-failure message to the user (Error ID kept).

        The graph's ``route_terminal_failure`` already persisted RequestStatus.ERROR
        and built the ``Error ID: <correlation_id>`` message; we send the failure
        notification (parity with the legacy terminal path). Preserves the
        academic-paper paywall-specific copy when the graph carried that reason.
        """
        if request.effective_silent:
            return
        # The runner classifies the terminal exception into a user-facing message
        # type (extraction/content-fetch failures -> ``empty_content`` instead of
        # the misleading ``processing_failed`` LLM-parse copy). Default preserves
        # the historical ``processing_failed`` behaviour when absent.
        error_type = "processing_failed"
        if isinstance(final_state, dict):
            error_type = str(final_state.get("notification_type") or "processing_failed")
        await self.summary_delivery.send_processing_failure(
            message=request.message,
            url_text=request.url_text,
            correlation_id=request.correlation_id,
            silent=request.silent,
            batch_mode=request.batch_mode,
            error_type=error_type,
        )

    async def _notify_orchestration_failure(self, request: URLFlowRequest, exc: Exception) -> None:
        """Notify on a facade-orchestration failure (outside the graph terminal path).

        Preserves the AcademicPaperUnavailableError paywall-specific copy
        (url_processor.py ~456-473) for paper failures the extraction adapter
        surfaces; falls back to the generic processing_failed template otherwise.
        """
        if request.silent or request.batch_mode:
            return
        from app.adapters.academic.platform_extractor import AcademicPaperUnavailableError

        if isinstance(exc, AcademicPaperUnavailableError):
            paper_details = (
                f"{exc.host.upper()} paper unavailable ({exc.reason}). Neither the "
                f"abstract nor the PDF could be reached -- this paper is likely behind "
                f"a login or paywall, or the host's anti-bot is blocking this request."
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

    async def _mark_request_error(self, request: URLFlowRequest, req_id: int | None) -> None:
        """Safety-net: ensure the request is marked ERROR on an orchestration failure."""
        if req_id is None:
            return
        try:
            from app.domain.models.request import RequestStatus

            await self.request_repo.async_update_request_status(req_id, RequestStatus.ERROR)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "graph_failed_to_update_request_status_on_error",
                extra={"cid": request.correlation_id, "req_id": req_id},
            )

    async def _record_url_flow_start(self, *, request: URLFlowRequest, req_id: int) -> None:
        """Write a ``running`` job row at flow entry for crash-recovery (lease)."""
        if not request.manage_processing_job:
            return
        try:
            from app.infrastructure.persistence.request_processing_job_repository import (
                RequestProcessingJobRepository,
            )

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
                "graph_url_flow_job_start_record_failed",
                extra={"cid": request.correlation_id, "req_id": req_id, "error": str(exc)},
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
        """Emit the per-request wall-time metric + record the terminal job row."""
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
                "graph_url_flow_metric_emit_failed",
                extra={"cid": request.correlation_id, "error": str(exc)},
            )

        if elapsed_seconds >= slow_threshold:
            logger.warning(
                "graph_url_flow_slow_request",
                extra={
                    "cid": request.correlation_id,
                    "req_id": req_id,
                    "elapsed_seconds": round(elapsed_seconds, 1),
                    "status": status,
                    "url": redact_url_for_logging(request.url_text),
                },
            )

        if req_id is None or not request.manage_processing_job:
            return
        try:
            from app.infrastructure.persistence.request_processing_job_repository import (
                RequestProcessingJobRepository,
            )

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
                "graph_url_flow_job_outcome_record_failed",
                extra={"cid": request.correlation_id, "req_id": req_id, "error": str(exc)},
            )


class _SummaryResultStub:
    """Minimal duck-typed stand-in for ``InteractiveSummaryResult`` for delivery."""

    __slots__ = ("llm_result", "model_used", "served_from_cache", "summary")

    def __init__(
        self,
        *,
        summary: dict[str, Any] | None,
        llm_result: Any,
        served_from_cache: bool,
        model_used: str | None,
    ) -> None:
        self.summary = summary
        self.llm_result = llm_result
        self.served_from_cache = served_from_cache
        self.model_used = model_used


def _summary_for_delivery(summary_json: Any, *, suppress_tldr_ru: bool) -> Any:
    """Return the summary to render, optionally without the inline ``tldr_ru``.

    When a full Russian block will follow (bilingual delivery), the inline
    TL;DR (RU) section is redundant, so it is stripped from a shallow copy of the
    primary-card summary. The original dict is left untouched.
    """
    if suppress_tldr_ru and isinstance(summary_json, dict) and "tldr_ru" in summary_json:
        return {k: v for k, v in summary_json.items() if k != "tldr_ru"}
    return summary_json


def _message_identity(message: Any) -> tuple[int | None, int | None, int | None]:
    """Extract ``(chat_id, user_id, input_message_id)`` at the Telegram boundary.

    Production messages expose ``from_user.id`` (the owner) and ``chat.id`` -- NOT
    ``sender.id`` / ``sender_id`` / ``chat_id``. Reads them via the canonical
    ``safe_telegram_*`` validators exactly like the legacy lifecycle
    (``platform_extraction/lifecycle.py`` + ``content_extractor.py``) so the
    request row's owner ``user_id`` is populated and the IDOR ownership filter
    (rule 12) holds on every graph-path row.
    """
    if message is None:
        return None, None, None
    chat_obj = getattr(message, "chat", None)
    chat_id = safe_telegram_chat_id(
        getattr(chat_obj, "id", None) if chat_obj is not None else None,
        field_name="chat_id",
    )
    from_user_obj = getattr(message, "from_user", None)
    user_id = safe_telegram_user_id(
        getattr(from_user_obj, "id", None) if from_user_obj is not None else None,
        field_name="user_id",
    )
    msg_id_raw = getattr(message, "id", getattr(message, "message_id", None))
    input_message_id = safe_message_id(msg_id_raw, field_name="message_id")
    return chat_id, user_id, input_message_id


def _message_identity_flexible(message: Any) -> tuple[int | None, int | None, int | None]:
    """Extract Telegram identity from production messages and lightweight test wrappers."""
    chat_id, user_id, input_message_id = _message_identity(message)
    if message is None:
        return chat_id, user_id, input_message_id
    if chat_id is None:
        chat_obj = getattr(message, "chat", None)
        chat_id = safe_telegram_chat_id(
            getattr(message, "chat_id", None)
            or getattr(message, "peer_id", None)
            or (getattr(chat_obj, "id", None) if chat_obj is not None else None),
            field_name="chat_id",
        )
    if user_id is None:
        sender = getattr(message, "sender", None) or getattr(message, "from_user", None)
        user_id = safe_telegram_user_id(
            getattr(message, "sender_id", None)
            or getattr(message, "from_id", None)
            or (getattr(sender, "id", None) if sender is not None else None),
            field_name="user_id",
        )
    return chat_id, user_id, input_message_id
