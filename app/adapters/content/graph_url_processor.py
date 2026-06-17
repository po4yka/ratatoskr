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

from app.adapters.content.url_flow_models import (
    URLFlowContext,
    URLFlowRequest,
    URLProcessingFlowResult,
)
from app.application.graphs.summarize.graph import (
    DEFAULT_RECURSION_LIMIT,
    build_initial_state,
    invocation_config,
)
from app.core.async_utils import raise_if_cancelled
from app.core.lang import LANG_RU, choose_language
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.core.summary_contract_impl.quality_metadata import merge_summary_quality_metadata
from app.core.url_utils import compute_dedupe_hash
from app.core.validation import (
    safe_message_id,
    safe_telegram_chat_id,
    safe_telegram_user_id,
)

# Route version for the URL graph path -- mirrors the legacy ``URL_ROUTE_VERSION``
# the lifecycle stamps on ``requests.route_version`` so graph-path rows are
# indistinguishable from the legacy path (no code model default; a plain int).
from app.adapters.content.content_extractor import URL_ROUTE_VERSION

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
        # The streamed runner (``run_summarize_graph_streamed``) lives in
        # ``app.di.graphs`` -- injected as a callable so the adapter never imports
        # the ``app.di`` tier (layered-architecture contract). The plain
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
        correlation_id = request.correlation_id or ""
        # No persistence target for the content-only path (no request row / silent
        # summarization); the graph persist node short-circuits every DB write when
        # request_id is None. ``0`` is NOT a usable sentinel -- it is a real FK value
        # and INSERTing a Summary against requests.id=0 raises ForeignKeyViolationError
        # (audit #1).
        initial_state = build_initial_state(
            correlation_id=correlation_id,
            request_id=None,
            lang=lang,
            input_url="",
            source_text=content_text,
        )
        config = invocation_config(
            correlation_id=correlation_id or "content-only",
            recursion_limit=DEFAULT_RECURSION_LIMIT,
        )
        try:
            final_state = await self._graph.ainvoke(initial_state, config=config)
        except Exception as exc:
            raise_if_cancelled(exc)
            # audit #4: RE-RAISE rather than swallow to ``{}``. The background
            # retry runner (``run_with_backoff``) only retries a stage that RAISES,
            # so returning ``{}`` here bypassed the configured retry_attempts. The
            # caller (run_stage) wraps this in a StageError; the retry loop fires.
            logger.warning(
                "graph_content_only_summarize_failed",
                extra={"cid": correlation_id, "error": str(exc)},
            )
            raise

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
        await self._complete_metadata_and_enrich(summary, content_text, lang, correlation_id)

        # Apply the request quality metadata the graph nodes do not set (parity
        # with PureSummaryService._apply_request_quality_metadata).
        merge_summary_quality_metadata(
            summary,
            source_coverage=request.source_coverage,
            extraction_quality=request.extraction_quality,
            extraction_confidence=request.extraction_confidence,
        )
        return summary

    async def _complete_metadata_and_enrich(
        self,
        summary: dict[str, Any],
        content_text: str,
        lang: str,
        correlation_id: str,
    ) -> None:
        """LLM metadata-completion + RAG-field enrichment for the content-only path.

        Ports legacy ``ensure_summary_payload``'s two completion steps (audit #5)
        the content-only graph path lost. Port-safe: the LLM call goes through the
        ``llm_client`` port; the RAG enrichment is a pure ``app.core`` computation.
        The content-only path has no request row, so the metadata-completion LLM
        call is NOT persisted (request_id=None would FK-violate, audit #1) -- the
        legacy path persisted it against a real request id, the content-only callers
        (handlers/rss) own their own request row + summary persistence downstream.
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
                    completed, _record = await complete_summary_metadata_via_llm(
                        llm_client=llm_client,
                        content_text=content_text,
                        fields=missing,
                        request_id=None,
                        correlation_id=correlation_id,
                        structured_output_mode=getattr(
                            self.cfg.openrouter, "structured_output_mode", None
                        ),
                    )
                    for key, value in completed.items():
                        if value and key in missing:
                            metadata[key] = value
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

            # Parity concern 9 -- bot_reply_message_id update (delivery concern).
            await self._persist_bot_reply(request, req_id, result)

            # The graph re-detects the content language during extract; read it back
            # from final_state so the chosen output lang + RU-translation gating match
            # the legacy context builder (which fed both off ``extraction.detected_lang``)
            # rather than the pre-graph preferred_lang.
            detected_lang = self._detected_lang(final_state)
            chosen_lang = choose_language(
                getattr(self.cfg.runtime, "preferred_lang", None), detected_lang
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
                    url_hash=compute_dedupe_hash(request.url_text),
                )

            return result
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
        return bool(getattr(self.cfg.runtime, "summary_streaming_enabled", True))

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
        running extraction: ``async_create_request`` upserts on ``dedupe_hash`` so a
        repeat URL resolves the existing row id, and ``persist_message_snapshot``
        writes the ``telegram_messages`` row + User/Chat upserts (persist-everything).
        The graph's extract node attaches its crawl results / failures to this
        request_id.

        ``chat_id`` / ``user_id`` / ``input_message_id`` are extracted at the
        Telegram boundary via ``safe_telegram_*`` exactly like the legacy lifecycle
        (``from_user.id`` / ``chat.id`` / message id) -- a NULL ``user_id`` here would
        break the defense-in-depth IDOR ownership filter (rule 12).
        """
        from app.core.url_utils import normalize_url

        dedupe_hash = compute_dedupe_hash(request.url_text)
        normalized = normalize_url(request.url_text)
        chat_id, user_id, input_message_id = _message_identity(request.message)
        req_id = await self.message_persistence.request_repo.async_create_request(
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
        # persist-everything: snapshot the originating Telegram message (and upsert
        # the sending User / Chat) so the graph path is row-for-row identical to the
        # legacy lifecycle. Best-effort, mirroring legacy: a snapshot failure must
        # not abort the flow (the request row already exists).
        if request.message is not None:
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
        self, request: URLFlowRequest, req_id: int, result: URLProcessingFlowResult
    ) -> None:
        """Deliver the summary + persist bot_reply_message_id via the delivery service.

        The delivery service owns both the structured-summary send and the
        bot_reply_message_id persistence (parity concern 9). We build the minimal
        context it needs from the graph result.
        """
        if request.effective_silent:
            return
        context = self._context_for_delivery(request, req_id, result)
        from app.adapters.content.url_flow_models import create_chunk_llm_stub

        summary_result = _SummaryResultStub(
            summary=result.summary_json,
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
        await self.summary_delivery.send_processing_failure(
            message=request.message,
            url_text=request.url_text,
            correlation_id=request.correlation_id,
            silent=request.silent,
            batch_mode=request.batch_mode,
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

        if req_id is None:
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
