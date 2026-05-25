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
from typing import Any

from taskiq import TaskiqDepends

from app.config import AppConfig  # noqa: TC001 — taskiq resolves type hints at runtime
from app.core.logging_utils import get_logger
from app.db.session import Database  # noqa: TC001 — taskiq resolves type hints at runtime
from app.tasks.broker import broker
from app.tasks.deps import get_app_config, get_db

logger = get_logger(__name__)


# ── Taskiq dependency provider for URLProcessingTaskRuntime ──────────────────


async def _get_url_processing_runtime(
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
) -> "URLProcessingTaskRuntime":
    """Build the URL-processing task runtime once per worker process."""
    global _url_processing_runtime_instance
    if _url_processing_runtime_instance is None:
        _url_processing_runtime_instance = _build_url_processing_runtime(cfg, db)
    return _url_processing_runtime_instance


_url_processing_runtime_instance: "URLProcessingTaskRuntime | None" = None


# ── Task ─────────────────────────────────────────────────────────────────────


@broker.task(task_name="ratatoskr.url.process")
async def process_url_request(
    request_id: int,
    cfg: AppConfig = TaskiqDepends(get_app_config),
    db: Database = TaskiqDepends(get_db),
    runtime: "URLProcessingTaskRuntime" = TaskiqDepends(_get_url_processing_runtime),
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
    runtime: "URLProcessingTaskRuntime",
) -> None:
    from app.api.background.durable_jobs import RequestProcessingJobRepository

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
        await _run_url_task(
            request_id=request_id,
            cid=cid,
            job_repo=job_repo,
            job=job,
            lease_owner=lease_owner,
            cfg=cfg,
            db=db,
            runtime=runtime,
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
    runtime: "URLProcessingTaskRuntime",
) -> None:
    from app.api.background.durable_jobs import RequestProcessingJobRepository

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
        request_id=request_id,
    )
    logger.info(
        "process_url_request_succeeded",
        extra={"request_id": request_id, "cid": cid},
    )


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

    async with db.session() as session:
        row = await session.scalar(select(Summary).where(Summary.request_id == request_id))
        if row is None:
            return None
        return {"json_payload": row.json_payload or {}}


def _format_summary_for_edit(
    summary_data: dict[str, Any] | None,
    *,
    cid: str | None,
    runtime: "URLProcessingTaskRuntime",
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

        sections = build_card_sections(
            summary_json,
            None,  # llm stub — not available at edit time; model info line is skipped when None
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
    runtime: "URLProcessingTaskRuntime",
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
    runtime: "URLProcessingTaskRuntime",
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
    except Exception:
        pass


def _make_worker_message(
    *,
    chat_id: int | None,
    reply_message_id: int | None,
) -> "_WorkerMessageStub":
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
    def peer_id(self) -> "_WorkerPeerStub":
        return _WorkerPeerStub(chat_id=self.chat_id)


class _WorkerPeerStub:
    def __init__(self, *, chat_id: int | None) -> None:
        self.channel_id = chat_id
        self.chat_id = chat_id
        self.user_id = chat_id


# ── Runtime dataclass ─────────────────────────────────────────────────────────


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


def _build_url_processing_runtime(cfg: AppConfig, db: Database) -> URLProcessingTaskRuntime:
    """Construct the URL-processing task runtime from config and DB.

    Called once per worker process (cached in module-level singleton).
    """
    from app.adapters.telegram.worker_telegram_sender import WorkerTelegramSender
    from app.di.repositories import (
        build_crawl_result_repository,
        build_llm_repository,
        build_request_repository,
        build_summary_repository,
        build_user_repository,
    )
    from app.adapters.llm import LLMClientFactory
    from app.di.shared import LazySemaphoreFactory, build_response_formatter, build_url_processor
    from app.adapters.content.scraper.factory import ContentScraperFactory

    audit_func: Any = lambda *_a, **_kw: None  # noqa: E731
    sem_factory = LazySemaphoreFactory(cfg.runtime.max_concurrent_calls)
    llm_client = LLMClientFactory.create_from_config(cfg, audit=audit_func)
    response_formatter = build_response_formatter(cfg)

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
    )

    telegram_sender = WorkerTelegramSender(bot_token=cfg.telegram.bot_token)

    return URLProcessingTaskRuntime(
        url_processor=url_processor,
        telegram_sender=telegram_sender,
        response_formatter=response_formatter,
    )
