"""URL handling for Telegram bot messages."""

from __future__ import annotations

import asyncio
import inspect
from typing import TYPE_CHECKING, Any, cast

from app.adapters.telegram.batch_relationship_analysis_service import (
    BatchRelationshipAnalysisService,
)
from app.adapters.telegram.url_batch_policy_service import URLBatchPolicyService
from app.adapters.telegram.url_batch_processor import (
    BatchProcessingResult,
    BatchProcessRequest,
    URLBatchProcessor,
)
from app.adapters.telegram.url_state_store import URLAwaitingStateStore
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.core.url_utils import extract_all_urls, normalize_url
from app.core.verbosity import VerbosityLevel
from app.security.file_validation import FileValidationError, SecureFileValidator

if TYPE_CHECKING:
    from app.adapters.content.graph_url_processor import GraphURLProcessor as URLProcessor
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.application.ports.requests import RequestRepositoryPort
    from app.application.ports.users import UserRepositoryPort
    from app.application.services.adaptive_timeout import AdaptiveTimeoutService
    from app.config import AppConfig
    from app.core.verbosity import VerbosityResolver
    from app.db.session import Database

logger = get_logger(__name__)


class _NullRepository:
    def __getattr__(self, _name: str) -> Any:  # pragma: no cover - defensive only
        raise AttributeError(_name)


class URLHandler:
    """Telegram-layer orchestrator for URL message processing.

    Responsibilities:
    - Batch policy enforcement: delegates to URLBatchPolicyService
    - Awaiting-URL state: delegates to URLAwaitingStateStore
    - Batch processing: delegates to URLBatchProcessor
    - Document file handling (.txt uploads → URL lists)
    - Security checks (URL validation, rate limits) via ResponseFormatter
    - Wires above into the correct sequence for each Telegram message path

    Boundary with the URL-flow facade (app/adapters/content/graph_url_processor.py):
      URLHandler owns Telegram UX (message replies, user state, batch policy).
      GraphURLProcessor owns content extraction and summarization (drives the
      summarize graph: scraper chain, LLM calls, DB persistence). URLHandler calls
      it for the single-URL extraction+summarization step; all Telegram-specific
      logic (progress messages, awaiting state, translation commands) stays here.
    """

    def __init__(
        self,
        db: Database | None,
        response_formatter: ResponseFormatter,
        url_processor: URLProcessor,
        adaptive_timeout_service: AdaptiveTimeoutService | None = None,
        verbosity_resolver: VerbosityResolver | None = None,
        llm_client: Any | None = None,
        batch_session_repo: Any | None = None,
        batch_config: Any | None = None,
        user_repo: UserRepositoryPort | None = None,
        request_repo: RequestRepositoryPort | None = None,
        batch_policy: URLBatchPolicyService | None = None,
        awaiting_state: URLAwaitingStateStore | None = None,
        file_validator: SecureFileValidator | None = None,
        batch_processor: URLBatchProcessor | None = None,
        relationship_analysis_service: BatchRelationshipAnalysisService | None = None,
        cfg: AppConfig | None = None,
    ) -> None:
        self.db = db
        self._cfg = cfg
        self.user_repo = user_repo or _NullRepository()
        self.request_repo = request_repo or _NullRepository()
        self.response_formatter = response_formatter
        self.url_processor = url_processor
        self._llm_client = llm_client
        self._adaptive_timeout = adaptive_timeout_service
        self.verbosity_resolver = verbosity_resolver
        self._file_validator = file_validator or SecureFileValidator()
        self._batch_policy = batch_policy or URLBatchPolicyService()
        self._awaiting_state = awaiting_state or URLAwaitingStateStore(ttl_sec=120)
        self._relationship_analysis_service = (
            relationship_analysis_service
            or BatchRelationshipAnalysisService(
                summary_repo=url_processor.summary_repo,
                batch_session_repo=batch_session_repo,
                llm_client=llm_client,
                batch_config=batch_config,
                response_formatter=response_formatter,
            )
        )
        self._batch_processor = batch_processor or URLBatchProcessor(
            response_formatter=self.response_formatter,
            request_repo=self.request_repo,
            user_repo=self.user_repo,
            summary_repo=url_processor.summary_repo,
            audit_func=url_processor.audit_func,
            relationship_analysis_service=self._relationship_analysis_service,
        )

        # Detect once at construction whether handle_url_flow uses the legacy
        # positional signature (message, url_text, **kwargs) used by test
        # monkeypatches, or the modern URLFlowRequest-based signature.
        # Avoids repeated inspect.signature() calls on every URL dispatch.
        _sig = inspect.signature(url_processor.handle_url_flow)
        _positional = [
            p
            for p in _sig.parameters.values()
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        self._url_processor_legacy_dispatch: bool = len(_positional) >= 2

        # Test/introspection seam: exposes the awaiting-state mapping directly.
        self._awaiting_url_users = self._awaiting_state.raw_state

    async def _compute_url_timeout(self, url: str, attempt: int = 0) -> float:
        """Compatibility wrapper around URL batch timeout policy."""
        return await self._batch_policy.compute_timeout(
            url=url,
            attempt=attempt,
            adaptive_timeout_service=self._adaptive_timeout,
        )

    async def apply_url_security_checks(
        self,
        message: Any,
        urls: list[str],
        uid: int,
        correlation_id: str,
    ) -> list[str]:
        """Validate a URL list using the shared batch policy."""
        return await self._batch_policy.apply_security_checks(
            message=message,
            urls=urls,
            uid=uid,
            correlation_id=correlation_id,
            response_formatter=self.response_formatter,
        )

    async def process_url_batch(
        self,
        message: Any,
        urls: list[str],
        uid: int,
        correlation_id: str,
        *,
        interaction_id: int | None = None,
        start_time: float | None = None,
        initial_message_id: int | None = None,
    ) -> BatchProcessingResult | None:
        """Process a URL batch through the dedicated batch processor."""
        max_concurrent = max(2, min(self._batch_policy.max_concurrent, len(urls)))
        return await self._batch_processor.execute_batch(
            BatchProcessRequest(
                message=message,
                urls=urls,
                uid=uid,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
                start_time=start_time,
                initial_message_id=initial_message_id,
                max_concurrent=max_concurrent,
                max_retries=self._batch_policy.max_retries,
                compute_timeout=self._compute_url_timeout,
                handle_single_url=self.handle_single_url,
            )
        )

    async def add_awaiting_user(self, uid: int) -> None:
        """Add user to awaiting URL list."""
        await self._awaiting_state.add(uid)

    async def cancel_pending_requests(self, uid: int) -> bool:
        """Cancel any pending URL requests for a user. Returns whether the user was awaiting."""
        return await self._awaiting_state.remove(uid)

    async def handle_awaited_url(
        self,
        message: Any,
        text: str,
        uid: int,
        correlation_id: str,
        interaction_id: int,
        start_time: float,
    ) -> None:
        """Handle URL sent after /summarize command."""
        from app.utils.typing_indicator import send_typing_once

        await send_typing_once(self.response_formatter, message)

        urls = extract_all_urls(text)
        await self._awaiting_state.consume(uid)

        urls = await self.apply_url_security_checks(message, urls, uid, correlation_id)
        if not urls:
            return

        if len(urls) > 1:
            progress_message_id = await self._create_batch_progress_message(
                message=message,
                urls_count=len(urls),
                text_template="🚀 Preparing to process {count} links...",
            )
            await self.process_url_batch(
                message,
                urls,
                uid,
                correlation_id,
                interaction_id=interaction_id,
                start_time=start_time,
                initial_message_id=progress_message_id,
            )
            return

        await self.handle_single_url(
            message=message,
            url=urls[0],
            correlation_id=correlation_id,
            interaction_id=interaction_id,
        )

    async def handle_direct_url(
        self,
        message: Any,
        text: str,
        uid: int,
        correlation_id: str,
        interaction_id: int,
        start_time: float,
    ) -> None:
        """Handle direct URL message with security validation."""
        from app.utils.typing_indicator import send_typing_once

        await send_typing_once(self.response_formatter, message)

        urls = extract_all_urls(text)
        urls = await self.apply_url_security_checks(message, urls, uid, correlation_id)
        if not urls:
            return

        if len(urls) > 1:
            progress_message_id = await self._create_batch_progress_message(
                message=message,
                urls_count=len(urls),
                text_template="Processing {count} links in parallel...",
            )
            await self.process_url_batch(
                message,
                urls,
                uid,
                correlation_id,
                interaction_id=interaction_id,
                start_time=start_time,
                initial_message_id=progress_message_id,
            )
            return

        await self.handle_single_url(
            message=message,
            url=urls[0],
            correlation_id=correlation_id,
            interaction_id=interaction_id,
        )

    async def is_awaiting_url(self, uid: int) -> bool:
        """Check if user is awaiting a URL (respects TTL)."""
        return await self._awaiting_state.contains(uid)

    async def cleanup_expired_state(self) -> int:
        """Remove expired awaiting entries. Returns count removed."""
        return await self._awaiting_state.cleanup_expired()

    def can_handle_document(self, message: Any) -> bool:
        """Return True when the message contains a supported .txt batch file."""
        document = getattr(message, "document", None)
        if not document or not hasattr(document, "file_name"):
            return False
        file_name = getattr(document, "file_name", "")
        return isinstance(file_name, str) and file_name.lower().endswith(".txt")

    async def handle_document_file(
        self,
        message: Any,
        correlation_id: str,
        interaction_id: int,
        start_time: float,
    ) -> None:
        """Handle .txt file processing (files containing URLs)."""
        file_path: str | None = None
        try:
            file_path = await self._download_document_file(message)
            if not file_path:
                await self.response_formatter.send_error_notification(
                    message,
                    "unexpected_error",
                    correlation_id,
                    details="Failed to download the uploaded file from Telegram servers.",
                )
                return

            try:
                urls = await self._parse_txt_file(file_path)
            except FileValidationError as exc:
                logger.error(
                    "file_validation_failed",
                    extra={"error": str(exc), "cid": correlation_id},
                )
                await self.response_formatter.safe_reply(
                    message,
                    f"❌ File validation failed: {exc!s}",
                )
                return

            if not urls:
                await self.response_formatter.send_error_notification(
                    message,
                    "no_urls_found",
                    correlation_id,
                    details="No valid links starting with http:// or https:// were detected in the file.",
                )
                return

            uid = self._resolve_user_id(message)
            valid_urls = await self.apply_url_security_checks(message, urls, uid, correlation_id)
            if not valid_urls:
                return

            await self.response_formatter.safe_reply(
                message,
                f"📄 File accepted. Processing {len(valid_urls)} links.",
            )
            try:
                initial_gap = max(
                    0.12,
                    (self.response_formatter.MIN_MESSAGE_INTERVAL_MS + 10) / 1000.0,
                )
                await asyncio.sleep(initial_gap)
            except Exception as exc:
                raise_if_cancelled(exc)

            progress_message_id: int | None = None
            if not self._is_draft_streaming_enabled():
                progress_message_id = await self.response_formatter.safe_reply_with_id(
                    message,
                    f"🔄 Preparing to process {len(valid_urls)} links...",
                )
            logger.debug(
                "document_file_processing_started",
                extra={"url_count": len(valid_urls)},
            )

            await self.process_url_batch(
                message,
                valid_urls,
                uid,
                correlation_id,
                interaction_id=interaction_id,
                start_time=start_time,
                initial_message_id=progress_message_id,
            )

            try:
                min_gap_sec = max(
                    0.6,
                    (self.response_formatter.MIN_MESSAGE_INTERVAL_MS + 50) / 1000.0,
                )
                await asyncio.sleep(min_gap_sec)
            except Exception as exc:
                raise_if_cancelled(exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("document_file_processing_error", extra={"cid": correlation_id})
            await self.response_formatter.send_error_notification(
                message,
                "unexpected_error",
                correlation_id,
                details="An error occurred while parsing or downloading the uploaded file.",
            )
        finally:
            if file_path:
                await self._cleanup_downloaded_file(file_path, correlation_id)

    async def handle_single_url(
        self,
        *,
        message: Any,
        url: str,
        correlation_id: str,
        interaction_id: int | None = None,
        batch_mode: bool = False,
        on_phase_change: Any | None = None,
        progress_tracker: Any | None = None,
    ) -> Any:
        # Worker enqueue path: when enabled and not in batch mode, persist the
        # request row, send a placeholder reply, enqueue the Taskiq task, and
        # return immediately.  The worker will edit the placeholder with the
        # final summary once processing is complete.
        if (
            not batch_mode
            and self._cfg is not None
            and getattr(self._cfg.runtime, "url_worker_enqueue_enabled", False)
        ):
            return await self._handle_single_url_enqueue(
                message=message,
                url=url,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
            )

        resolved_progress_tracker = progress_tracker
        if resolved_progress_tracker is None and not batch_mode:
            resolved_progress_tracker = await self._resolve_progress_tracker(message)
        from app.adapters.content.url_flow_models import URLFlowRequest

        flow_request = URLFlowRequest(
            message=message,
            url_text=url,
            correlation_id=correlation_id,
            interaction_id=interaction_id,
            batch_mode=batch_mode,
            on_phase_change=on_phase_change,
            progress_tracker=resolved_progress_tracker,
        )
        handle_url_flow = self.url_processor.handle_url_flow

        # Legacy tests monkeypatch ``handle_url_flow(message, url_text, **kwargs)`` on the
        # processor instance. Preserve that contract while keeping the production
        # URLFlowRequest-based call path as the default.
        # Dispatch style was detected once at __init__ time via inspect.signature
        # and stored in self._url_processor_legacy_dispatch to avoid per-call overhead.
        if self._url_processor_legacy_dispatch:
            legacy_handle_url_flow = cast("Any", handle_url_flow)
            return await legacy_handle_url_flow(
                message,
                url,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
                batch_mode=batch_mode,
                on_phase_change=on_phase_change,
                progress_tracker=resolved_progress_tracker,
            )

        return await handle_url_flow(flow_request)

    async def _handle_single_url_enqueue(
        self,
        *,
        message: Any,
        url: str,
        correlation_id: str,
        interaction_id: int | None = None,
    ) -> Any:
        """Persist request, send placeholder, enqueue Taskiq task.

        Called from ``handle_single_url`` when ``url_worker_enqueue_enabled``
        is True and the request is not part of a batch.  Returns immediately
        after enqueueing; the worker edits the placeholder with the final
        summary.
        """
        from app.adapters.content.url_flow_models import URLProcessingFlowResult
        from app.api.background.durable_jobs import RequestProcessingJobRepository
        from app.core.url_utils import compute_dedupe_hash, normalize_url
        from app.domain.models.request import RequestStatus
        from app.observability.metrics import record_url_enqueue, set_url_processing_queue_depth

        cid = correlation_id
        try:
            normalized = normalize_url(url)
            dedupe_hash = compute_dedupe_hash(normalized)
        except Exception:
            normalized = url
            dedupe_hash = None

        # Resolve chat_id and user_id from the Telethon message object.
        chat_id: int | None = None
        user_id: int | None = None
        try:
            peer = getattr(message, "peer_id", None)
            if peer is not None:
                chat_id = (
                    getattr(peer, "channel_id", None)
                    or getattr(peer, "chat_id", None)
                    or getattr(peer, "user_id", None)
                )
            if chat_id is None:
                chat_id = getattr(message, "chat_id", None)
            from_user = getattr(message, "from_user", None) or getattr(message, "sender", None)
            user_id = int(getattr(from_user, "id", 0) or 0) or None
        except Exception:
            pass

        input_message_id: int | None = None
        try:
            input_message_id = int(getattr(message, "id", None) or 0) or None
        except Exception:
            pass

        # 1. Persist the request row.
        request_repo = self.request_repo
        try:
            request_id, created_new = await request_repo.async_create_request_once(
                type_="url",
                status=RequestStatus.PENDING,
                correlation_id=cid,
                chat_id=chat_id,
                user_id=user_id,
                input_url=url,
                normalized_url=normalized,
                dedupe_hash=dedupe_hash,
                input_message_id=input_message_id,
                initial_attempt_trigger="initial",
            )
        except Exception as exc:
            logger.error(
                "url_worker_enqueue_request_create_failed",
                extra={"cid": cid, "error": str(exc)},
            )
            record_url_enqueue(status="error")
            # Fall back to inline processing.
            from app.adapters.content.url_flow_models import URLFlowRequest

            flow_request = URLFlowRequest(
                message=message,
                url_text=url,
                correlation_id=cid,
                interaction_id=interaction_id,
                batch_mode=False,
            )
            return await self.url_processor.handle_url_flow(flow_request)

        if not created_new:
            record_url_enqueue(status="duplicate")
            logger.info(
                "url_worker_enqueue_duplicate_suppressed",
                extra={"cid": cid, "request_id": request_id, "chat_id": chat_id},
            )
            return URLProcessingFlowResult(success=True, request_id=request_id)

        message_persistence = getattr(self.url_processor, "message_persistence", None)
        persist_snapshot = getattr(message_persistence, "persist_message_snapshot", None)
        if callable(persist_snapshot):
            try:
                await persist_snapshot(request_id, message)
            except Exception as exc:
                logger.warning(
                    "url_worker_enqueue_message_snapshot_failed",
                    extra={"cid": cid, "request_id": request_id, "error": str(exc)},
                )

        # 2. Insert the pending job row.
        job_repo = RequestProcessingJobRepository(self.db)
        try:
            await job_repo.record_pending_enqueue(
                request_id=request_id,
                correlation_id=cid,
            )
        except Exception as exc:
            logger.warning(
                "url_worker_enqueue_job_row_failed",
                extra={"cid": cid, "request_id": request_id, "error": str(exc)},
            )

        # 3. Send the placeholder reply and persist its message_id.
        placeholder_text = f"Processing... (Error ID: {cid})"
        bot_reply_message_id: int | None = None
        try:
            bot_reply_message_id = await self.response_formatter.safe_reply_with_id(
                message, placeholder_text
            )
        except Exception as exc:
            logger.warning(
                "url_worker_enqueue_placeholder_send_failed",
                extra={"cid": cid, "request_id": request_id, "error": str(exc)},
            )

        if bot_reply_message_id is not None:
            try:
                await request_repo.async_update_bot_reply_message_id(
                    request_id, bot_reply_message_id
                )
            except Exception as exc:
                logger.warning(
                    "url_worker_enqueue_bot_reply_id_update_failed",
                    extra={"cid": cid, "request_id": request_id, "error": str(exc)},
                )

        # 4. Enqueue the Taskiq task.
        from app.tasks.url_processing import (  # lazy: avoids eager taskiq type-hint resolution
            process_url_request,
        )

        try:
            await (
                process_url_request.kicker()
                .with_task_id(f"url-{request_id}-{cid or 'nocid'}")
                .kiq(request_id=request_id)
            )
        except Exception as exc:
            logger.error(
                "url_worker_enqueue_kiq_failed",
                extra={"cid": cid, "request_id": request_id, "error": str(exc)},
            )
            record_url_enqueue(status="error")
            return URLProcessingFlowResult(success=False)

        # 5. Record metrics.
        record_url_enqueue(status="success")
        try:
            depth = await job_repo.pending_count()
            set_url_processing_queue_depth(depth)
        except Exception:
            pass

        logger.info(
            "url_worker_enqueued",
            extra={"cid": cid, "request_id": request_id, "chat_id": chat_id},
        )
        return URLProcessingFlowResult(success=True, request_id=request_id)

    async def translate_summary_to_ru(
        self,
        summary: dict[str, Any],
        *,
        req_id: int,
        correlation_id: str | None = None,
        url_hash: str | None = None,
        source_lang: str | None = None,
    ) -> str | None:
        return await self.url_processor.post_summary_tasks.translate_summary_to_ru(
            summary,
            req_id=req_id,
            correlation_id=correlation_id,
            url_hash=url_hash,
            source_lang=source_lang,
        )

    async def clear_extraction_cache(self) -> int:
        return await self.url_processor.content_extractor.clear_cache()

    async def _resolve_progress_tracker(self, message: Any) -> Any | None:
        if not self.verbosity_resolver:
            return None

        verbosity = await self.verbosity_resolver.get_verbosity(message)
        if verbosity != VerbosityLevel.READER:
            return None

        return self.response_formatter.progress_tracker

    async def _create_batch_progress_message(
        self,
        *,
        message: Any,
        urls_count: int,
        text_template: str,
    ) -> int | None:
        if self._is_draft_streaming_enabled():
            return None

        return await self.response_formatter.safe_reply_with_id(
            message,
            text_template.format(count=urls_count),
        )

    def _is_draft_streaming_enabled(self) -> bool:
        sender = getattr(self.response_formatter, "sender", self.response_formatter)
        checker = getattr(sender, "is_draft_streaming_enabled", None)
        if not callable(checker):
            return False

        try:
            enabled = checker()
        except Exception:
            return False

        return enabled if isinstance(enabled, bool) else False

    def _resolve_user_id(self, message: Any) -> int:
        from_user = getattr(message, "from_user", None)
        user_id = getattr(from_user, "id", 0)
        return int(user_id) if isinstance(user_id, int) else 0

    async def _download_document_file(self, message: Any) -> str | None:
        try:
            document = getattr(message, "document", None)
            if not document:
                return None
            file_info = await message.download()
            return str(file_info) if file_info else None
        except Exception as exc:
            logger.error("file_download_failed", extra={"error": str(exc)})
            return None

    async def _parse_txt_file(self, file_path: str) -> list[str]:
        lines = await asyncio.to_thread(self._file_validator.safe_read_text_file, file_path)

        urls: list[str] = []
        skipped_count = 0
        for line_num, line in enumerate(lines, 1):
            line = line.strip()

            if not line or line.startswith("#"):
                continue

            if line.startswith(("http://", "https://")):
                if " " in line or "\t" in line:
                    logger.warning(
                        "suspicious_url_skipped",
                        extra={
                            "url_preview": line[:50],
                            "reason": "contains whitespace",
                            "line_num": line_num,
                        },
                    )
                    skipped_count += 1
                    continue

                try:
                    normalized = normalize_url(line)
                    urls.append(normalized)
                except ValueError as exc:
                    logger.warning(
                        "invalid_url_in_file",
                        extra={
                            "url_preview": line[:50],
                            "error": str(exc),
                            "line_num": line_num,
                            "file_path": file_path,
                        },
                    )
                    skipped_count += 1
            elif line.startswith(("http", "www")):
                logger.warning(
                    "malformed_url_skipped",
                    extra={
                        "url_preview": line[:50],
                        "reason": "invalid protocol",
                        "line_num": line_num,
                    },
                )
                skipped_count += 1

        logger.info(
            "file_parsed_successfully",
            extra={
                "file_path": file_path,
                "urls_found": len(urls),
                "urls_skipped": skipped_count,
                "lines_read": len(lines),
            },
        )
        return urls

    async def _cleanup_downloaded_file(self, file_path: str, correlation_id: str) -> None:
        cleanup_attempts = 0
        max_cleanup_attempts = 3
        while cleanup_attempts < max_cleanup_attempts:
            try:
                await asyncio.to_thread(self._file_validator.cleanup_file, file_path)
                return
            except PermissionError as exc:
                cleanup_attempts += 1
                if cleanup_attempts >= max_cleanup_attempts:
                    logger.error(
                        "file_cleanup_permission_denied",
                        extra={
                            "error": str(exc),
                            "file_path": file_path,
                            "cid": correlation_id,
                            "attempts": cleanup_attempts,
                        },
                    )
                else:
                    await asyncio.sleep(0.1 * cleanup_attempts)
            except FileNotFoundError:
                return
            except Exception as exc:
                cleanup_attempts += 1
                logger.error(
                    "file_cleanup_unexpected_error",
                    extra={
                        "error": str(exc),
                        "file_path": file_path,
                        "cid": correlation_id,
                        "error_type": type(exc).__name__,
                        "attempts": cleanup_attempts,
                    },
                )
                return
