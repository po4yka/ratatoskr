"""Taskiq task: process a URL request submitted by the bot.

The bot persists the request row, sends a placeholder Telegram message, and
enqueues this task.  The task leases the job row, runs the full URL processing
pipeline (scrape + LLM + summary persistence), then edits the placeholder
message with the final summary text.  On retry, when a ``summaries`` row
already exists for the ``request_id``, the LLM step is skipped and only the
Telegram edit is executed (idempotency guard).
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

from taskiq import TaskiqDepends, TaskiqEvents

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.tasks.broker import broker
from app.tasks.deps import get_app_config, get_db

logger = get_logger(__name__)


class LeaseLostError(RuntimeError):
    """Raised when a worker can no longer prove it owns a durable-job lease."""


# ── Runtime dataclass ─────────────────────────────────────────────────────────
# Defined before the @broker.task functions below: taskiq resolves task type
# hints eagerly at decoration (import) time via get_type_hints, so this name
# must already be in module scope when `process_url_request` is decorated.


class URLProcessingTaskRuntime:
    """Holds the per-worker-process singletons needed by the URL task."""

    def __init__(
        self,
        *,
        url_processor: Any,
        telegram_sender: Any,
        response_formatter: Any,
    ) -> None:
        self.url_processor = url_processor
        self.telegram_sender = telegram_sender
        self.response_formatter = response_formatter


# ── Taskiq dependency provider for URLProcessingTaskRuntime ──────────────────


async def _get_url_processing_runtime(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> URLProcessingTaskRuntime:
    """Build the URL-processing task runtime once per worker process."""
    global _url_processing_runtime_instance
    if _url_processing_runtime_instance is None:
        checkpointer = await _start_url_processing_checkpointer(cfg)
        _url_processing_runtime_instance = _build_url_processing_runtime(
            cfg,
            db,
            checkpointer=checkpointer,
        )
    return _url_processing_runtime_instance


_url_processing_runtime_instance: URLProcessingTaskRuntime | None = None
_url_processing_checkpointer_runtime: Any | None = None
_credential_refresh_task: asyncio.Task[None] | None = None


async def _start_url_processing_checkpointer(cfg: AppConfig) -> Any | None:
    """Start the worker-local durable saver and return it when enabled."""
    global _url_processing_checkpointer_runtime
    if not cfg.langgraph_checkpoint.enabled:
        return None
    if _url_processing_checkpointer_runtime is not None:
        return _url_processing_checkpointer_runtime.saver

    try:
        from app.infrastructure.checkpointing import CheckpointerRuntime

        runtime = CheckpointerRuntime(cfg=cfg)
        await runtime.start()
    except ImportError:
        logger.warning("langgraph_checkpointer_not_installed")
        return None
    except Exception:
        logger.exception("langgraph_checkpointer_startup_failed")
        return None

    _url_processing_checkpointer_runtime = runtime
    return runtime.saver


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _stop_url_processing_checkpointer(_state: Any) -> None:
    """Close the worker-local checkpointer pool during Taskiq shutdown."""
    global _url_processing_checkpointer_runtime
    runtime = _url_processing_checkpointer_runtime
    _url_processing_checkpointer_runtime = None
    if runtime is not None:
        await runtime.stop(timeout=10.0)


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _start_credential_refresh(_state: Any) -> None:
    """Start the per-process credential hot-reload loop.

    A credential saved through the web UI lands in the API process; this
    worker's LLM clients live here. Mirrors bot.py: poll CredentialStore and
    swap changes into this process's ConfigHolder (``get_app_config``) so a
    rotated key reaches live task runs without a worker restart. Skipped when
    no owner is configured, exactly like bot.py.
    """
    global _credential_refresh_task
    cfg = await get_app_config()
    owner_id = next(iter(cfg.telegram.allowed_user_ids), None)
    if owner_id is None:
        return

    from app.config.credential_reloader import start_credential_refresh_task
    from app.infrastructure.persistence.credential_store import CredentialStore

    db = await get_db(cfg)
    _credential_refresh_task = start_credential_refresh_task(
        cfg,  # type: ignore[arg-type]  # actually the ConfigHolder get_app_config hands out
        CredentialStore(db),
        owner_id=owner_id,
    )


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _stop_credential_refresh(_state: Any) -> None:
    """Cancel the credential refresh loop during Taskiq shutdown."""
    global _credential_refresh_task
    task = _credential_refresh_task
    _credential_refresh_task = None
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


# ── Task ─────────────────────────────────────────────────────────────────────


@broker.task(task_name="ratatoskr.url.process", retry_on_error=True, max_retries=3)
async def process_url_request(
    request_id: int,
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
    runtime: URLProcessingTaskRuntime = TaskiqDepends(_get_url_processing_runtime),
) -> None:
    """Process a single URL request submitted by the bot.

    Args:
        request_id: ``requests.id`` row persisted by the bot before enqueue.
    """
    await _process_url_request_body(request_id=request_id, cfg=cfg, db=db, runtime=runtime)


# ── Task body (separated for direct testability) ─────────────────────────────


async def _process_url_request_body(
    *,
    request_id: int,
    cfg: AppConfig,
    db: Database,
    runtime: URLProcessingTaskRuntime,
) -> None:
    from app.infrastructure.persistence.request_processing_job_repository import (
        RequestProcessingJobRepository,
    )

    job_repo = RequestProcessingJobRepository(db)
    lease_owner = f"worker:taskiq:{request_id}"
    lease_ttl = int(getattr(cfg.runtime, "url_flow_lease_ttl_sec", 900))

    # Acquire the lease on this specific pending row.
    job = await job_repo.lease_next(
        lease_owner=lease_owner,
        lease_ttl_seconds=lease_ttl,
        by_id=request_id,
    )
    if job is None:
        logger.warning(
            "url_task_lease_not_acquired",
            extra={"request_id": request_id},
        )
        return

    cid = job.correlation_id
    logger.info(
        "process_url_request_started",
        extra={"request_id": request_id, "lease_owner": lease_owner, "cid": cid},
    )

    try:
        await _run_url_task_with_lease_renewal(
            request_id=request_id,
            cid=cid,
            job_repo=job_repo,
            job=job,
            lease_owner=lease_owner,
            cfg=cfg,
            db=db,
            runtime=runtime,
            lease_ttl_seconds=lease_ttl,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error_code = type(exc).__name__
        error_message = str(exc)[:2000]
        logger.exception(
            "process_url_request_failed",
            extra={
                "request_id": request_id,
                "cid": cid,
                "error_code": error_code,
            },
        )
        retry_delay = int(getattr(cfg.background, "durable_retry_delay_seconds", 30))
        await job_repo.mark_failed(
            job,
            lease_owner=lease_owner,
            error_code=error_code,
            error_message=error_message,
            retry_delay_seconds=retry_delay,
        )
        # Notify the user of the failure via the placeholder message.
        await _try_edit_placeholder(
            request_id=request_id,
            text=f"Processing failed (Error ID: {cid or request_id}). Please try again.",
            cid=cid,
            db=db,
            runtime=runtime,
        )


async def _run_url_task(
    *,
    request_id: int,
    cid: str | None,
    job_repo: Any,
    job: Any,
    lease_owner: str,
    cfg: AppConfig,
    db: Database,
    runtime: URLProcessingTaskRuntime,
) -> None:

    # Load the request row to get chat_id, input_url, and other metadata.
    request_data = await _load_request(request_id=request_id, db=db)
    if request_data is None:
        logger.error(
            "url_task_request_not_found",
            extra={"request_id": request_id, "cid": cid},
        )
        await job_repo.mark_failed(
            job,
            lease_owner=lease_owner,
            error_code="REQUEST_NOT_FOUND",
            error_message=f"No requests row for id={request_id}",
            retry_delay_seconds=0,
        )
        return

    chat_id: int | None = request_data.get("chat_id")
    url_text: str | None = request_data.get("input_url")
    bot_reply_message_id: int | None = request_data.get("bot_reply_message_id")

    if not url_text:
        logger.error(
            "url_task_missing_input_url",
            extra={"request_id": request_id, "cid": cid},
        )
        await job_repo.mark_failed(
            job,
            lease_owner=lease_owner,
            error_code="MISSING_INPUT_URL",
            error_message="Request has no input_url",
            retry_delay_seconds=0,
        )
        return

    # Idempotency: if a summary already exists, skip the LLM step.
    summary_data = await _load_summary(request_id=request_id, db=db)
    if summary_data is not None:
        logger.info(
            "url_task_summary_already_exists",
            extra={"request_id": request_id, "cid": cid},
        )
        summary_text = _format_summary_for_edit(summary_data, cid=cid, runtime=runtime)
        if chat_id is not None and bot_reply_message_id is not None:
            await _edit_placeholder(
                chat_id=chat_id,
                message_id=bot_reply_message_id,
                text=summary_text,
                cid=cid,
                runtime=runtime,
            )
        await job_repo.mark_succeeded(
            job.id,
            lease_owner=lease_owner,
            lease_token=job.lease_token,
            request_id=request_id,
        )
        return

    # Run the full URL processing pipeline (silent=True so URLProcessor does
    # not attempt to send via the Telethon formatter; we edit the placeholder
    # ourselves after the summary is persisted).
    from app.adapters.content.url_flow_models import URLFlowRequest

    flow_request = URLFlowRequest(
        message=_make_worker_message(chat_id=chat_id, reply_message_id=bot_reply_message_id),
        url_text=url_text,
        correlation_id=cid,
        silent=True,  # Suppress Telegram delivery — worker edits the placeholder directly.
        batch_mode=False,
        existing_request_id=request_id,
        manage_processing_job=False,
        persist_message_snapshot=False,
    )

    try:
        result = await runtime.url_processor.handle_url_flow(flow_request)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise

    success = getattr(result, "success", False)
    if not success:
        raise RuntimeError("URLProcessor.handle_url_flow returned failure result")

    # Reload the summary that the pipeline just persisted.
    summary_data = await _load_summary(request_id=request_id, db=db)
    summary_text = _format_summary_for_edit(summary_data, cid=cid, runtime=runtime)

    if chat_id is not None and bot_reply_message_id is not None:
        await _edit_placeholder(
            chat_id=chat_id,
            message_id=bot_reply_message_id,
            text=summary_text,
            cid=cid,
            runtime=runtime,
        )

    await job_repo.mark_succeeded(
        job.id,
        lease_owner=lease_owner,
        lease_token=job.lease_token,
        request_id=request_id,
    )
    logger.info(
        "process_url_request_succeeded",
        extra={"request_id": request_id, "cid": cid},
    )


async def _run_url_task_with_lease_renewal(
    *,
    job_repo: Any,
    job: Any,
    lease_owner: str,
    lease_ttl_seconds: int,
    **kwargs: Any,
) -> None:
    """Run URL processing while renewing its lease before it can expire."""
    processing_task = asyncio.create_task(
        _run_url_task(job_repo=job_repo, job=job, lease_owner=lease_owner, **kwargs)
    )
    renewal_interval = max(1.0, lease_ttl_seconds / 3)
    try:
        while True:
            done, _ = await asyncio.wait({processing_task}, timeout=renewal_interval)
            if done:
                await processing_task
                return
            renewed = await job_repo.renew_lease(
                job_id=job.id,
                lease_owner=lease_owner,
                lease_token=job.lease_token,
                lease_ttl_seconds=lease_ttl_seconds,
            )
            if renewed:
                continue
            processing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await processing_task
            raise LeaseLostError(f"lease lost for request_id={job.request_id}")
    except asyncio.CancelledError:
        processing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await processing_task
        raise


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _load_request(*, request_id: int, db: Database) -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.db.models.core import Request

    async with db.session() as session:
        row = await session.scalar(select(Request).where(Request.id == request_id))
        if row is None:
            return None
        return {
            "chat_id": row.chat_id,
            "input_url": row.input_url,
            "bot_reply_message_id": row.bot_reply_message_id,
            "correlation_id": row.correlation_id,
        }


async def _load_summary(*, request_id: int, db: Database) -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.db.models.core import Summary

    from app.db.models.core import LLMCall

    async with db.session() as session:
        row = await session.scalar(select(Summary).where(Summary.request_id == request_id))
        if row is None:
            return None
        # The worker has no LLM handle at edit time, so the model that actually
        # produced this summary is read back from its call trail -- otherwise
        # the card footer silently loses the model name. Highest attempt_index
        # is the call that succeeded: the repair loop only advances on retry,
        # so the last one is the one whose output was persisted.
        model = await session.scalar(
            select(LLMCall.model)
            .where(LLMCall.request_id == request_id, LLMCall.model.is_not(None))
            .order_by(LLMCall.attempt_index.desc())
            .limit(1)
        )
        return {"json_payload": row.json_payload or {}, "model": model}


def _format_summary_for_edit(
    summary_data: dict[str, Any] | None,
    *,
    cid: str | None,
    runtime: URLProcessingTaskRuntime,
) -> str:
    """Format a persisted summary dict into Telegram message text.

    Uses the same ``build_card_sections`` path as the bot so the worker
    produces identical output to the non-silent bot path.  Falls back to a
    minimal placeholder only when the summary JSON is absent.
    """
    if summary_data is None:
        return f"Summary ready (Error ID: {cid or 'unknown'})"

    summary_json = summary_data.get("json_payload") or {}
    if not summary_json:
        return f"Summary ready (Error ID: {cid or 'unknown'})"

    try:
        from app.adapters.external.formatting.summary.card_renderer import build_card_sections

        # build_card_sections only reads `.model` off this, so a namespace
        # carrying the model recovered from llm_calls restores the footer
        # without giving the worker a real LLM client it has no use for.
        model_name = summary_data.get("model")
        llm_stub = SimpleNamespace(model=model_name) if model_name else None

        sections = build_card_sections(
            summary_json,
            llm_stub,
            None,  # chunks
            reader=False,
            text_processor=runtime.response_formatter._text_processor,
            data_formatter=runtime.response_formatter._data_formatter,
            lang=runtime.response_formatter._lang,
        )
        text = "\n\n".join(sections).strip()
        if text:
            return text
    except Exception:
        logger.warning("format_summary_card_failed", exc_info=True)

    return f"Summary ready (Error ID: {cid or 'unknown'})"


async def _edit_placeholder(
    *,
    chat_id: int,
    message_id: int,
    text: str,
    cid: str | None,
    runtime: URLProcessingTaskRuntime,
) -> None:
    try:
        await runtime.telegram_sender.edit_message_text(
            chat_id,
            message_id,
            text,
            cid=cid,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "url_task_edit_placeholder_failed",
            extra={"chat_id": chat_id, "message_id": message_id, "cid": cid, "error": str(exc)},
        )


async def _try_edit_placeholder(
    *,
    request_id: int,
    text: str,
    cid: str | None,
    db: Database,
    runtime: URLProcessingTaskRuntime,
) -> None:
    """Best-effort edit of the placeholder on error — never raises."""
    try:
        request_data = await _load_request(request_id=request_id, db=db)
        if request_data is None:
            return
        chat_id = request_data.get("chat_id")
        bot_reply_message_id = request_data.get("bot_reply_message_id")
        if chat_id is None or bot_reply_message_id is None:
            return
        await _edit_placeholder(
            chat_id=chat_id,
            message_id=bot_reply_message_id,
            text=text,
            cid=cid,
            runtime=runtime,
        )
    except Exception as exc:
        # Best-effort notify; never raises -- but a swallowed failure here means
        # the user is left with a stale placeholder, so make it observable.
        logger.warning(
            "try_edit_placeholder_failed",
            extra={"request_id": request_id, "cid": cid, "error": str(exc)},
        )


def _make_worker_message(
    *,
    chat_id: int | None,
    reply_message_id: int | None,
) -> _WorkerMessageStub:
    """Create a minimal message stub for the worker's URLProcessor call."""
    return _WorkerMessageStub(chat_id=chat_id, reply_message_id=reply_message_id)


class _WorkerMessageStub:
    """Minimal stand-in for a Telethon message object in the worker path.

    URLProcessor.handle_url_flow is called with ``silent=True`` so delivery
    calls (``send_structured_summary_response``) are skipped.  The fields
    below are the only ones that the pipeline may access during the
    extraction/LLM phase.
    """

    def __init__(self, *, chat_id: int | None, reply_message_id: int | None) -> None:
        self.chat_id = chat_id
        self.id = reply_message_id  # mimic Telethon message.id

    # Telethon messages expose peer_id.channel_id / chat_id via various paths.
    @property
    def peer_id(self) -> _WorkerPeerStub:
        return _WorkerPeerStub(chat_id=self.chat_id)


class _WorkerPeerStub:
    def __init__(self, *, chat_id: int | None) -> None:
        self.channel_id = chat_id
        self.chat_id = chat_id
        self.user_id = chat_id


def _build_url_processing_runtime(
    cfg: AppConfig,
    db: Database,
    *,
    checkpointer: Any | None = None,
) -> URLProcessingTaskRuntime:
    """Construct the URL-processing task runtime from config and DB.

    Called once per worker process (cached in module-level singleton).
    """
    from app.adapters.content.scraper.factory import ContentScraperFactory
    from app.adapters.llm import LLMClientFactory
    from app.adapters.telegram.worker_telegram_sender import WorkerTelegramSender
    from app.di.repositories import (
        build_crawl_result_repository,
        build_llm_repository,
        build_request_repository,
        build_summary_repository,
        build_user_repository,
    )
    from app.di.search import build_search_dependencies
    from app.di.shared import LazySemaphoreFactory, build_response_formatter, build_url_processor

    audit_func: Any = lambda *_a, **_kw: None  # noqa: E731
    sem_factory = LazySemaphoreFactory(cfg.runtime.max_concurrent_calls)
    llm_client = LLMClientFactory.create_from_config(cfg, audit=audit_func)
    # cfg is this worker process's ConfigHolder (see get_app_config); llm_client
    # froze its api_key/model at construction, so re-apply the swapped config on
    # every credential-refresh tick (_start_credential_refresh) to this
    # long-lived per-process client. Mirrors app/di/shared.py's
    # build_core_dependencies -- this runtime builds its own llm_client instead
    # of going through that helper, so the same wiring is repeated here.
    register_listener = getattr(cfg, "register_listener", None)
    apply_runtime_config = getattr(llm_client, "apply_runtime_config", None)
    if callable(register_listener) and callable(apply_runtime_config):
        register_listener(apply_runtime_config)
    response_formatter = build_response_formatter(cfg)

    # The worker single-URL path is the PRIMARY summarize entrypoint
    # (url_worker_enqueue_enabled defaults true), so it must wire the vector
    # store + embedding service into the facade exactly like the Telegram/CLI
    # path. Without them the graph builds a QdrantSummaryIndexAdapter(None, None),
    # silently no-opping ADR-0012 read-your-writes freshness on the live path.
    search = build_search_dependencies(cfg, db, llm_client=llm_client, audit_func=audit_func)

    url_processor = build_url_processor(
        cfg=cfg,
        db=db,
        firecrawl=ContentScraperFactory.create_from_config(cfg, audit=audit_func),
        openrouter=llm_client,
        response_formatter=response_formatter,
        audit_func=audit_func,
        sem=sem_factory,
        request_repo=build_request_repository(db),
        summary_repo=build_summary_repository(db),
        crawl_result_repo=build_crawl_result_repository(db),
        llm_repo=build_llm_repository(db),
        user_repo=build_user_repository(db),
        vector_store=search.vector_store,
        embedding_service=search.embedding_service,
        checkpointer=checkpointer,
    )

    telegram_sender = WorkerTelegramSender(bot_token=cfg.telegram.bot_token)

    return URLProcessingTaskRuntime(
        url_processor=url_processor,
        telegram_sender=telegram_sender,
        response_formatter=response_formatter,
    )
