"""Batch URL processing for Telegram flows."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )

from app.adapter_models.batch_processing import URLBatchStatus, URLStatus
from app.adapters.external.formatting import BatchProgressFormatter
from app.adapters.telegram.batch_sender_utils import (
    is_draft_streaming_enabled as _is_draft_streaming_enabled,
    resolve_sender as _resolve_sender,
    send_message_draft_safe as _send_message_draft_safe,
)
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import generate_correlation_id
from app.core.url_utils import compute_dedupe_hash, normalize_url
from app.db.user_interactions import async_safe_update_user_interaction
from app.domain.models.request import RequestStatus
from app.utils.progress_tracker import ProgressTracker

logger = get_logger(__name__)


async def _await_if_needed(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


@dataclass(slots=True)
class BatchProcessRequest:
    message: Any
    urls: list[str]
    uid: int
    correlation_id: str
    interaction_id: int | None = None
    start_time: float | None = None
    initial_message_id: int | None = None
    max_concurrent: int = 4
    max_retries: int = 2
    compute_timeout: Callable[[str, int], Awaitable[float]] | None = None
    handle_single_url: Callable[..., Coroutine[Any, Any, Any]] | None = None


@dataclass(slots=True)
class BatchProcessingResult:
    batch_status: URLBatchStatus
    url_to_request_id: dict[str, int]
    correlation_id: str
    uid: int


@dataclass(slots=True)
class _CachedSummaryDelivery:
    url: str
    payload: dict[str, Any]
    request_id: int


@dataclass(slots=True)
class _BatchRunState:
    request: BatchProcessRequest
    batch_status: URLBatchStatus
    url_to_request_id: dict[str, int]
    cached_summaries: list[_CachedSummaryDelivery]
    semaphore: asyncio.Semaphore
    sender: Any
    draft_enabled: bool
    failed_domains: set[str] = field(default_factory=set)
    domain_failure_counts: dict[str, int] = field(default_factory=dict)
    domain_events: dict[str, asyncio.Event] = field(default_factory=dict)
    delivery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    edit_consecutive_failures: int = 0
    circuit_breaker_opened_at: float = 0.0
    rate_limited_until: float = 0.0
    initial_message_id: int | None = None
    progress_tracker: ProgressTracker | None = None


class URLBatchProcessor:
    """Executes URL batch processing with caching, progress, and fail-fast semantics."""

    _DOMAIN_FAILFAST_THRESHOLD = 2
    _EDIT_CIRCUIT_BREAKER_THRESHOLD = 3

    def __init__(
        self,
        *,
        response_formatter: ResponseFormatter,
        request_repo: Any,
        user_repo: Any,
        summary_repo: Any,
        audit_func: Any | None = None,
        relationship_analysis_service: Any | None = None,
    ) -> None:
        self._response_formatter = response_formatter
        self._request_repo = request_repo
        self._user_repo = user_repo
        self._summary_repo = summary_repo
        self._audit = audit_func
        self._relationship_analysis_service = relationship_analysis_service

    async def execute_batch(
        self, batch_request: BatchProcessRequest
    ) -> BatchProcessingResult | None:
        """Execute a batch URL-processing request."""
        if not batch_request.urls:
            return None

        batch_status = URLBatchStatus.from_urls(batch_request.urls)
        batch_status.concurrency = batch_request.max_concurrent

        state = _BatchRunState(
            request=batch_request,
            batch_status=batch_status,
            url_to_request_id={},
            cached_summaries=[],
            # URL-pipeline-level concurrency only (scrape + summarize per link).
            # This sits OUTSIDE the global LLM-call semaphore acquired inside
            # LLMResponseWorkflow._invoke_llm, so it does NOT add to LLM-call
            # concurrency -- total concurrent LLM calls stay bounded by the one
            # shared global semaphore (MAX_CONCURRENT_CALLS) across all paths.
            semaphore=asyncio.Semaphore(batch_request.max_concurrent),
            sender=_resolve_sender(self._response_formatter),
            draft_enabled=_is_draft_streaming_enabled(_resolve_sender(self._response_formatter)),
            initial_message_id=batch_request.initial_message_id,
        )

        from app.utils.typing_indicator import typing_indicator

        async with typing_indicator(self._response_formatter, batch_request.message):
            await self._pre_register_urls(state)
            await asyncio.sleep(0.5)
            await self._ensure_initial_progress_message(state)
            await self._deliver_cached_summaries(state)

            state.progress_tracker = self._build_progress_tracker(state)
            progress_task = asyncio.create_task(state.progress_tracker.process_update_queue())
            heartbeat_task = asyncio.create_task(self._progress_heartbeat(state.progress_tracker))

            try:
                await self._process_all_urls(state)
            finally:
                heartbeat_task.cancel()
                state.progress_tracker.mark_complete()
                try:
                    async with asyncio.timeout(10.0):
                        await progress_task
                except Exception as exc:
                    raise_if_cancelled(exc)
                    logger.debug("progress_task_wait_failed", extra={"error": str(exc)})
                    progress_task.cancel()

            # Force one last progress edit showing final state before completion message
            if state.progress_tracker is not None:
                state.progress_tracker.force_update()
                await asyncio.sleep(0.3)

            await self._send_completion_message(state)
        await self._update_interaction(state)

        result = BatchProcessingResult(
            batch_status=state.batch_status,
            url_to_request_id=state.url_to_request_id,
            correlation_id=batch_request.correlation_id,
            uid=batch_request.uid,
        )
        if self._relationship_analysis_service is not None:
            try:
                await self._relationship_analysis_service.analyze_batch(
                    batch_result=result,
                    message=batch_request.message,
                )
            except Exception as exc:
                logger.warning(
                    "batch_relationship_analysis_failed",
                    extra={"error": str(exc), "cid": batch_request.correlation_id},
                )
        return result

    async def _pre_register_urls(self, state: _BatchRunState) -> None:
        chat_id = getattr(state.request.message.chat, "id", None)
        for url in state.request.urls:
            try:
                normalized = normalize_url(url)
                dedupe_hash = compute_dedupe_hash(url)
                if await self._load_cached_summary(state, url, dedupe_hash):
                    continue

                # In-flight dedupe: skip URLs with a recent processing/pending/error row.
                if hasattr(self._request_repo, "async_find_recent_request_by_dedupe"):
                    grace_sec = 60
                    existing = await _await_if_needed(
                        self._request_repo.async_find_recent_request_by_dedupe(
                            dedupe_hash, max_age_sec=grace_sec
                        )
                    )
                    if existing:
                        existing_status = existing.get("status")
                        existing_id = existing.get("id")
                        if existing_status in ("processing", "pending"):
                            logger.info(
                                "batch_url_dedupe_skip_in_flight",
                                extra={
                                    "url": url,
                                    "existing_request_id": existing_id,
                                    "uid": state.request.uid,
                                },
                            )
                            continue
                        if existing_status == "error":
                            logger.info(
                                "batch_url_dedupe_skip_recent_failure",
                                extra={
                                    "url": url,
                                    "existing_request_id": existing_id,
                                    "uid": state.request.uid,
                                },
                            )
                            continue

                request_id, is_new = await _await_if_needed(
                    self._request_repo.async_create_minimal_request(
                        type_="url",
                        status="pending",
                        correlation_id=generate_correlation_id(),
                        chat_id=chat_id,
                        user_id=state.request.uid,
                        input_url=url,
                        normalized_url=normalized,
                        dedupe_hash=dedupe_hash,
                    )
                )
                state.url_to_request_id[url] = request_id
                logger.debug(
                    "pre_registered_batch_url",
                    extra={
                        "url": url,
                        "request_id": request_id,
                        "is_new": is_new,
                        "uid": state.request.uid,
                    },
                )
            except Exception as exc:
                logger.warning(
                    "batch_url_pre_registration_failed",
                    extra={"url": url, "error": str(exc), "uid": state.request.uid},
                )

    async def _load_cached_summary(
        self,
        state: _BatchRunState,
        url: str,
        dedupe_hash: str,
    ) -> bool:
        existing_request = await _await_if_needed(
            self._request_repo.async_get_request_by_dedupe_hash(dedupe_hash)
        )
        if not existing_request or existing_request.get("status") != RequestStatus.COMPLETED:
            return False

        request_id = existing_request.get("id")
        summary = await _await_if_needed(
            self._summary_repo.async_get_summary_by_request(request_id)
        )
        if not summary:
            return False

        from app.adapters.content.url_processor import URLProcessingFlowResult

        payload = summary.get("json_payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                payload = {}
        title = URLProcessingFlowResult.from_summary(payload).title
        state.batch_status.mark_cached(url, title=title)
        if isinstance(payload, dict) and payload:
            state.cached_summaries.append(
                _CachedSummaryDelivery(url=url, payload=payload, request_id=request_id)
            )
            state.url_to_request_id[url] = request_id

        logger.debug(
            "batch_url_cache_hit",
            extra={"url": url, "request_id": request_id, "uid": state.request.uid},
        )
        if self._audit is not None:
            self._audit(
                "INFO",
                "batch_url_cache_hit",
                {"url": url, "request_id": request_id, "uid": state.request.uid},
            )
        return True

    async def _ensure_initial_progress_message(self, state: _BatchRunState) -> None:
        if state.initial_message_id is not None or state.draft_enabled:
            return
        try:
            initial_text = BatchProgressFormatter.format_progress_message(state.batch_status)
            state.initial_message_id = await self._response_formatter.safe_reply_with_id(
                state.request.message,
                initial_text,
                parse_mode="HTML",
            )
        except Exception as exc:
            logger.debug("initial_progress_message_failed", extra={"error": str(exc)})
            state.initial_message_id = None

    async def _deliver_cached_summaries(self, state: _BatchRunState) -> None:
        if not state.cached_summaries:
            return
        logger.info(
            "delivering_cached_summaries",
            extra={"count": len(state.cached_summaries), "uid": state.request.uid},
        )
        for cached_summary in state.cached_summaries:
            try:
                await self._response_formatter.send_cached_summary_notification(
                    state.request.message,
                    silent=False,
                )
                await self._response_formatter.send_structured_summary_response(
                    state.request.message,
                    cached_summary.payload,
                    llm=None,
                    summary_id=f"req:{cached_summary.request_id}",
                )
                await asyncio.sleep(0.3)
            except Exception as exc:
                logger.warning(
                    "cached_summary_delivery_failed",
                    extra={
                        "url": cached_summary.url,
                        "request_id": cached_summary.request_id,
                        "error": str(exc),
                    },
                )

    def _build_progress_tracker(self, state: _BatchRunState) -> ProgressTracker:
        progress_formatter = partial(self._format_progress_message, state)
        return ProgressTracker(
            total=len(state.request.urls),
            progress_formatter=progress_formatter,
            initial_message_id=state.initial_message_id,
            update_interval=1.0,
            small_batch_threshold=0,
            progress_threshold_percentage=25.0,
        )

    _CIRCUIT_BREAKER_RECOVERY_SEC = 15.0

    async def _format_progress_message(
        self,
        state: _BatchRunState,
        current: int,
        total_count: int,
        message_id: int | None,
    ) -> int | None:
        _ = current, total_count
        now = time.time()

        # Rate-limit backoff: skip if we're still in a cooldown window
        if now < state.rate_limited_until:
            return message_id

        # Circuit breaker: half-open recovery after cooldown
        if state.edit_consecutive_failures >= self._EDIT_CIRCUIT_BREAKER_THRESHOLD:
            if (now - state.circuit_breaker_opened_at) < self._CIRCUIT_BREAKER_RECOVERY_SEC:
                return message_id
            # Half-open: allow one probe attempt
            logger.debug("progress_circuit_breaker_half_open")

        try:
            progress_text = BatchProgressFormatter.format_progress_message(state.batch_status)
            draft_ok = await _send_message_draft_safe(
                state.sender,
                state.request.message,
                progress_text,
            )
            if draft_ok:
                state.edit_consecutive_failures = 0
                return message_id

            chat_id = getattr(state.request.message.chat, "id", None)
            if chat_id and message_id:
                edit_result = state.sender.edit_message(
                    chat_id,
                    message_id,
                    progress_text,
                    parse_mode="HTML",
                )
                edit_success = bool(await _await_if_needed(edit_result))
                if edit_success:
                    state.edit_consecutive_failures = 0
                    return message_id
                state.edit_consecutive_failures += 1
                if state.edit_consecutive_failures >= self._EDIT_CIRCUIT_BREAKER_THRESHOLD:
                    state.circuit_breaker_opened_at = time.time()
                    logger.warning(
                        "progress_edit_circuit_breaker_open",
                        extra={"consecutive_failures": state.edit_consecutive_failures},
                    )
            return message_id
        except Exception as exc:
            # Detect Telegram FloodWait / rate-limit errors
            flood_wait = getattr(exc, "value", None) or getattr(exc, "retry_after", None)
            if flood_wait and isinstance(flood_wait, (int, float)):
                state.rate_limited_until = time.time() + float(flood_wait)
                logger.info(
                    "progress_edit_rate_limited",
                    extra={"wait_sec": flood_wait},
                )
                return message_id

            state.edit_consecutive_failures += 1
            if state.edit_consecutive_failures >= self._EDIT_CIRCUIT_BREAKER_THRESHOLD:
                state.circuit_breaker_opened_at = time.time()
                logger.warning(
                    "progress_edit_circuit_breaker_open",
                    extra={
                        "consecutive_failures": state.edit_consecutive_failures,
                        "error": str(exc),
                    },
                )
            return message_id

    _HEARTBEAT_INTERVAL_SEC = 5.0
    _HEARTBEAT_SKIP_SEC = 3.0

    async def _progress_heartbeat(self, progress_tracker: ProgressTracker) -> None:
        while not progress_tracker.is_complete:
            try:
                await asyncio.sleep(self._HEARTBEAT_INTERVAL_SEC)
                # Skip if a regular update was queued recently
                if (time.time() - progress_tracker.last_queue_time) < self._HEARTBEAT_SKIP_SEC:
                    continue
                progress_tracker.force_update()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                raise_if_cancelled(exc)
                logger.debug("progress_heartbeat_failed", exc_info=True)

    async def _process_all_urls(self, state: _BatchRunState) -> None:
        all_tasks = [self._process_single_url(state, url) for url in state.request.urls]
        await asyncio.gather(*all_tasks, return_exceptions=True)

    async def _process_single_url(
        self,
        state: _BatchRunState,
        url: str,
    ) -> tuple[str, bool, str, str | None]:
        entry = state.batch_status._find_entry(url)
        if entry and entry.status == URLStatus.CACHED:
            await asyncio.sleep(0.5)
            await state.progress_tracker.increment_and_update()
            return url, True, "", entry.title

        async with state.semaphore:
            per_link_cid = generate_correlation_id()
            last_error = ""
            error_type = "unknown"
            start_time_ms = time.time() * 1000
            if entry is None:
                entry = state.batch_status._find_entry(url)
            url_domain = entry.domain if entry else None

            if url_domain and url_domain in state.failed_domains:
                processing_time_ms = time.time() * 1000 - start_time_ms
                skip_error = f"Skipped (domain {url_domain} timed out)"
                state.batch_status.mark_failed(
                    url,
                    error_type="domain_timeout",
                    error_message=skip_error,
                    processing_time_ms=processing_time_ms,
                )
                await self._update_request_error(
                    state,
                    url=url,
                    status="skipped",
                    error_type="domain_timeout",
                    error_message=skip_error,
                    processing_time_ms=processing_time_ms,
                )
                logger.info(
                    "domain_failfast_skipped",
                    extra={"url": url, "domain": url_domain, "uid": state.request.uid},
                )
                await state.progress_tracker.increment_and_update()
                return url, False, skip_error, None

            state.batch_status.mark_processing(url)
            phase_callback = partial(self._handle_phase_change, state, url)

            for attempt in range(state.request.max_retries + 1):
                if attempt > 0 and url_domain and url_domain in state.failed_domains:
                    last_error = f"Skipped (domain {url_domain} timed out)"
                    error_type = "domain_timeout"
                    break

                current_timeout = await self._compute_timeout(state, url, attempt)
                logger.debug(
                    "processing_link_parallel",
                    extra={
                        "uid": state.request.uid,
                        "url": url,
                        "cid": per_link_cid,
                        "attempt": attempt + 1,
                        "timeout_sec": current_timeout,
                    },
                )

                try:
                    result = await self._run_processing_attempt(
                        state=state,
                        url=url,
                        per_link_cid=per_link_cid,
                        url_domain=url_domain,
                        current_timeout=current_timeout,
                        phase_callback=phase_callback,
                    )
                    if result is None:
                        last_error = f"Skipped (domain {url_domain} timed out)"
                        error_type = "domain_timeout"
                        break

                    processing_time_ms = time.time() * 1000 - start_time_ms
                    title = getattr(result, "title", None) if result else None
                    state.batch_status.mark_complete(
                        url,
                        title=title,
                        processing_time_ms=processing_time_ms,
                    )
                    await self._deliver_summary_card(
                        state,
                        url=url,
                        result=result,
                        per_link_cid=per_link_cid,
                    )
                    await state.progress_tracker.increment_and_update()
                    return url, True, "", title
                except TimeoutError:
                    error_type = "timeout"
                    last_error = f"Timed out after {int(current_timeout)}s (ID: {per_link_cid[:8]})"
                    if attempt < state.request.max_retries:
                        state.batch_status.mark_retrying(
                            url,
                            attempt=attempt + 1,
                            max_retries=state.request.max_retries,
                        )
                        state.batch_status.mark_retry_waiting(url)
                        state.progress_tracker.force_update()
                        await asyncio.sleep(min(3.0 * (2**attempt), 60.0))
                        continue
                    self._record_domain_timeout(state, url_domain)
                except Exception as exc:
                    last_error = str(exc)
                    error_type, is_transient = self._classify_processing_error(last_error)
                    if is_transient and attempt < state.request.max_retries:
                        state.batch_status.mark_retrying(
                            url,
                            attempt=attempt + 1,
                            max_retries=state.request.max_retries,
                        )
                        state.progress_tracker.force_update()
                        await asyncio.sleep(min(3.0 * (2**attempt), 60.0))
                        continue
                    break

            processing_time_ms = time.time() * 1000 - start_time_ms
            state.batch_status.mark_failed(
                url,
                error_type=error_type,
                error_message=last_error,
                processing_time_ms=processing_time_ms,
            )
            await self._update_request_error(
                state,
                url=url,
                status=CallStatus.ERROR,
                error_type=error_type,
                error_message=last_error,
                processing_time_ms=processing_time_ms,
            )
            await state.progress_tracker.increment_and_update()
            return url, False, last_error, None

    async def _handle_phase_change(
        self,
        state: _BatchRunState,
        url: str,
        phase: str,
        title: str | None = None,
        content_length: int | None = None,
        model: str | None = None,
    ) -> None:
        if phase == "extracting":
            state.batch_status.mark_extracting(url)
        elif phase == "analyzing":
            state.batch_status.mark_analyzing(
                url,
                title=title,
                content_length=content_length,
                model=model,
            )
        elif phase == "summarizing":
            state.batch_status.mark_summarizing(url, model=model)
        elif phase == "retrying":
            state.batch_status.mark_retrying(url)
        elif phase == "waiting":
            state.batch_status.mark_retry_waiting(url)
        if state.progress_tracker is not None:
            state.progress_tracker.force_update()

    async def _compute_timeout(
        self,
        state: _BatchRunState,
        url: str,
        attempt: int,
    ) -> float:
        if state.request.compute_timeout is not None:
            return await state.request.compute_timeout(url, attempt)
        return min(450.0 * (1.5**attempt), 900.0)

    async def _run_processing_attempt(
        self,
        *,
        state: _BatchRunState,
        url: str,
        per_link_cid: str,
        url_domain: str | None,
        current_timeout: float,
        phase_callback: Callable[..., Awaitable[None]],
    ) -> Any | None:
        if state.request.handle_single_url is None:
            msg = "BatchProcessRequest.handle_single_url is required"
            raise RuntimeError(msg)
        processing_task: asyncio.Task[Any] = asyncio.create_task(
            state.request.handle_single_url(
                message=state.request.message,
                url=url,
                correlation_id=per_link_cid,
                batch_mode=True,
                on_phase_change=phase_callback,
            )
        )
        cancel_task = self._create_domain_cancel_task(state, url_domain)
        tasks_to_race: set[asyncio.Task[Any]] = {processing_task}
        if cancel_task is not None:
            tasks_to_race.add(cancel_task)

        try:
            done, pending = await asyncio.wait(
                tasks_to_race,
                timeout=current_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            processing_task.cancel()
            if cancel_task is not None:
                cancel_task.cancel()
            for task in (processing_task, cancel_task):
                if task is not None:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            raise

        await self._cancel_pending_tasks(pending)

        if not done:
            raise TimeoutError(f"Timed out after {int(current_timeout)}s (ID: {per_link_cid[:8]})")
        if cancel_task is not None and cancel_task in done:
            return None
        return processing_task.result()

    def _create_domain_cancel_task(
        self,
        state: _BatchRunState,
        url_domain: str | None,
    ) -> asyncio.Task[Any] | None:
        if not url_domain:
            return None
        return asyncio.create_task(self._get_domain_event(state, url_domain).wait())

    async def _cancel_pending_tasks(self, pending: set[asyncio.Task[Any]]) -> None:
        for task in pending:
            task.cancel()
        for task in pending:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _get_domain_event(self, state: _BatchRunState, domain: str) -> asyncio.Event:
        if domain not in state.domain_events:
            state.domain_events[domain] = asyncio.Event()
        return state.domain_events[domain]

    def _record_domain_timeout(self, state: _BatchRunState, url_domain: str | None) -> None:
        if not url_domain:
            return
        state.domain_failure_counts[url_domain] = state.domain_failure_counts.get(url_domain, 0) + 1
        if state.domain_failure_counts[url_domain] >= self._DOMAIN_FAILFAST_THRESHOLD:
            state.failed_domains.add(url_domain)
            self._get_domain_event(state, url_domain).set()

    def _classify_processing_error(self, last_error: str) -> tuple[str, bool]:
        transient_keywords = [
            "timeout",
            "connection",
            "network",
            "rate limit",
            "503",
            "502",
            "429",
        ]
        error_lower = last_error.lower()
        is_transient = any(keyword in error_lower for keyword in transient_keywords)
        if "timeout" in error_lower:
            return "timeout", is_transient
        if "connection" in error_lower or "network" in error_lower:
            return "network", is_transient
        if "429" in last_error or "rate limit" in error_lower:
            return "rate_limit", is_transient
        return "error", is_transient

    async def _deliver_summary_card(
        self,
        state: _BatchRunState,
        *,
        url: str,
        result: Any,
        per_link_cid: str,
    ) -> None:
        if not (
            result and getattr(result, "success", False) and getattr(result, "summary_json", None)
        ):
            return

        async with state.delivery_lock:
            try:
                request_id = getattr(result, "request_id", None) or state.url_to_request_id.get(url)
                await self._response_formatter.send_structured_summary_response(
                    state.request.message,
                    result.summary_json,
                    llm=None,
                    summary_id=f"req:{request_id}" if request_id else None,
                    correlation_id=per_link_cid,
                )
                await asyncio.sleep(0.5)
            except Exception as exc:
                logger.warning(
                    "batch_summary_card_delivery_failed",
                    extra={"url": url, "error": str(exc), "cid": per_link_cid},
                )

    async def _update_request_error(
        self,
        state: _BatchRunState,
        *,
        url: str,
        status: str,
        error_type: str,
        error_message: str,
        processing_time_ms: float,
    ) -> None:
        request_id = state.url_to_request_id.get(url)
        if not request_id:
            return
        try:
            await _await_if_needed(
                self._request_repo.async_update_request_error(
                    request_id=request_id,
                    status=status,
                    error_type=error_type,
                    error_message=error_message[:500] if error_message else None,
                    processing_time_ms=int(processing_time_ms),
                )
            )
        except Exception as exc:
            logger.warning(
                "failed_to_update_request_error",
                extra={"url": url, "request_id": request_id, "error": str(exc)},
            )

    async def _send_completion_message(self, state: _BatchRunState) -> None:
        completion_message = BatchProgressFormatter.format_completion_message(state.batch_status)
        logger.info(
            "sending_batch_completion",
            extra={
                "uid": state.request.uid,
                "total": len(state.request.urls),
                "success": state.batch_status.success_count,
            },
        )
        await self._response_formatter.safe_reply(
            state.request.message,
            completion_message,
            parse_mode="HTML",
        )

    async def _update_interaction(self, state: _BatchRunState) -> None:
        if state.request.interaction_id and state.request.start_time:
            await async_safe_update_user_interaction(
                self._user_repo,
                interaction_id=state.request.interaction_id,
                response_sent=True,
                response_type="batch_complete",
                start_time=state.request.start_time,
                logger_=logger,
            )
