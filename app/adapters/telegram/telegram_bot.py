from __future__ import annotations

import asyncio
import contextlib
import io
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.adapters.telegram.lifecycle_manager import TelegramLifecycleManager
from app.adapters.telegram.telethon_compat import normalize_parse_mode
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import generate_correlation_id, get_logger, setup_json_logging
from app.core.time_utils import UTC, format_iso_z
if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.config import AppConfig
    from app.db.session import Database
    from app.db.write_queue import DbWriteQueue

logger = get_logger(__name__)


# ...
@dataclass
class TelegramBot:
    """Refactored Telegram bot using modular components."""

    cfg: AppConfig
    db: Database
    runtime_builder: Callable[..., Any]
    audit_repository_builder: Callable[[Database], Any]
    db_write_queue: DbWriteQueue | None = None

    # Dynamically assigned in __post_init__ after runtime_builder()
    telegram_client: Any = field(default=None, init=False, repr=False)
    response_formatter: Any = field(default=None, init=False, repr=False)
    url_processor: Any = field(default=None, init=False, repr=False)
    forward_processor: Any = field(default=None, init=False, repr=False)
    message_handler: Any = field(default=None, init=False, repr=False)
    _ext_sem_obj: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        """Initialize bot components using the shared DI runtime."""
        setup_json_logging(self.cfg.runtime.log_level)
        logger.info(
            "bot_init",
            extra={
                "db_path": self.cfg.runtime.db_path,
                "log_level": self.cfg.runtime.log_level,
            },
        )

        self._audit_tasks: set[asyncio.Task[Any]] = set()
        self.audit_repo = self.audit_repository_builder(self.db)
        components = self.runtime_builder(
            cfg=self.cfg,
            db=self.db,
            safe_reply_func=self._safe_reply,
            reply_json_func=self._reply_json,
            db_write_queue=self.db_write_queue,
            audit_task_registry=self._audit_tasks,
        )
        self._runtime = components
        # Always use the multi-provider scraper chain; the chain already wraps a
        # FirecrawlProvider as one of its rungs and falls through to JS-rendering
        # providers when Firecrawl returns thin/empty content. Preferring
        # `firecrawl_client` here would bypass the chain entirely.
        self._firecrawl = components.core.scraper_chain
        self._llm_client = components.core.llm_client
        self._ext_sem_obj = None
        self._ext_sem_size = max(1, self.cfg.runtime.max_concurrent_calls)

        # Assign components and wire cross-component dependencies.
        self.telegram_client = components.telegram_client
        self.response_formatter = components.response_formatter
        self.url_processor = components.url_processor
        self.forward_processor = components.forward_processor
        self.message_handler = components.message_handler
        self.topic_searcher = components.search.topic_searcher
        self.local_searcher = components.search.local_searcher
        self.embedding_service = components.search.embedding_service
        self.vector_search_service = components.search.vector_search_service
        self.query_expansion_service = components.search.query_expansion_service
        self.hybrid_search_service = components.search.hybrid_search_service
        self.vector_store = components.search.vector_store
        self._application_services = components.application_services
        self._adaptive_timeout_service = components.adaptive_timeout_service

        self.message_handler.command_processor.runtime_state.url_processor = self.url_processor
        self.message_handler.url_processor = self.url_processor

        self._awaiting_url_users = self.message_handler.url_handler._awaiting_url_users

        # Lifecycle helpers for background startup/shutdown orchestration.
        self._lifecycle = TelegramLifecycleManager(self)
        self._backup_task: asyncio.Task[None] | None = None
        self._rate_limiter_cleanup_task: asyncio.Task[None] | None = None

    def _sem(self) -> asyncio.Semaphore:
        """Lazy-create a semaphore when an event loop is running.

        This avoids creating an asyncio.Semaphore at import/constructor time in tests
        that instantiate the bot without a running event loop.
        """
        runtime_sem_factory = getattr(getattr(self, "_runtime", None), "core", None)
        if runtime_sem_factory is not None:
            return self._runtime.core.semaphore_factory()
        if self._ext_sem_obj is None:
            self._ext_sem_obj = asyncio.Semaphore(self._ext_sem_size)
        return self._ext_sem_obj

    async def start(self) -> None:
        """Start the bot."""
        await self._lifecycle.on_startup()
        self._backup_task = self._lifecycle.backup_task
        self._rate_limiter_cleanup_task = self._lifecycle.rate_limiter_cleanup_task

        transcription_queue = getattr(self._runtime, "durable_transcription_queue", None)
        try:
            if transcription_queue is not None:
                await transcription_queue.reconcile_startup()
                await transcription_queue.start()
            await self.telegram_client.start(
                self.message_handler.handle_message,
                self.message_handler.handle_callback_query,
                self._build_reaction_handler(),
            )
        finally:
            if transcription_queue is not None:
                await transcription_queue.stop()
            await self._lifecycle.on_shutdown()

            # Close external clients and drain in-flight tasks
            await self._shutdown()

    def _build_reaction_handler(self) -> Callable[[Any], Awaitable[None]] | None:
        """Owner thumbs reaction on a summary -> +1/-1 feedback (best-effort).

        Returns None when no owner is configured. Reaction-update delivery to a
        bot is guaranteed only for reactions on the bot's own messages in 1:1
        DMs -- exactly this single-tenant owner case.
        """
        owner_ids = getattr(self.cfg.telegram, "allowed_user_ids", None) or ()
        if not owner_ids:
            return None
        from app.adapters.telegram.reaction_feedback import ReactionFeedbackHandler
        from app.infrastructure.persistence.repositories.summary_repository import (
            SummaryRepositoryAdapter,
        )

        recorder = ReactionFeedbackHandler(SummaryRepositoryAdapter(self.db), int(owner_ids[0]))
        return recorder.handle

    def _audit(self, level: str, event: str, details: dict) -> None:
        """Audit log helper (background async)."""
        if not hasattr(self, "audit_repo"):
            return

        async def _do_audit() -> None:
            try:
                await self.audit_repo.async_insert_audit_log(
                    log_level=level, event_type=event, details=details
                )
            except Exception as e:
                raise_if_cancelled(e)
                logger.warning("audit_persist_failed", extra={"error": str(e), "event": event})

        try:
            task = asyncio.create_task(_do_audit())
            # Keep a set of strong references to tasks to avoid them being GC'd
            if not hasattr(self, "_audit_tasks"):
                self._audit_tasks = set()
            self._audit_tasks.add(task)
            task.add_done_callback(self._audit_tasks.discard)
        except RuntimeError as exc:
            logger.debug("audit_task_schedule_skipped", extra={"error": str(exc)})
            return

    async def _shutdown(self, drain_timeout: float = 5.0) -> None:
        """Close external clients and drain in-flight tasks."""
        # 0. Close URL processor (drains background tasks)
        if hasattr(self, "url_processor") and hasattr(self.url_processor, "aclose"):
            try:
                await self.url_processor.aclose(timeout=drain_timeout)
            except Exception as e:
                raise_if_cancelled(e)
                logger.warning("shutdown_url_processor_close_failed", exc_info=True)

        # 1. Close the scraper chain (multi-provider; aclose propagates to all rungs)
        _core = getattr(getattr(self, "_runtime", None), "core", None)
        scraper_chain = getattr(_core, "scraper_chain", None)
        if scraper_chain is not None and hasattr(scraper_chain, "aclose"):
            try:
                async with asyncio.timeout(drain_timeout):
                    await scraper_chain.aclose()
            except Exception as e:
                raise_if_cancelled(e)
                logger.warning("shutdown_scraper_chain_close_failed", exc_info=True)

        # 2. Close LLM client
        llm_client = getattr(_core, "llm_client", None)
        if llm_client is not None and hasattr(llm_client, "aclose"):
            try:
                async with asyncio.timeout(drain_timeout):
                    await llm_client.aclose()
            except Exception as e:
                raise_if_cancelled(e)
                logger.warning("shutdown_llm_client_close_failed", exc_info=True)

        # 3. Close vector store
        vector_store = getattr(self, "vector_store", None)
        if vector_store is not None and hasattr(vector_store, "aclose"):
            try:
                async with asyncio.timeout(drain_timeout):
                    await vector_store.aclose()
            except Exception as e:
                raise_if_cancelled(e)
                logger.warning("shutdown_vector_store_close_failed", exc_info=True)

        # 4. Close embedding service
        embedding_service = getattr(self, "embedding_service", None)
        if embedding_service is not None and hasattr(embedding_service, "aclose"):
            try:
                async with asyncio.timeout(drain_timeout):
                    await embedding_service.aclose()
            except Exception as e:
                raise_if_cancelled(e)
                logger.warning("shutdown_embedding_service_close_failed", exc_info=True)

        # 5. Drain audit tasks
        audit_tasks: set[asyncio.Task[None]] = getattr(self, "_audit_tasks", set())
        if audit_tasks:
            with contextlib.suppress(Exception):
                async with asyncio.timeout(drain_timeout):
                    await asyncio.gather(*list(audit_tasks), return_exceptions=True)

        # Catch-all for orphaned OpenRouter connection pool entries that may
        # not be covered by _llm_client.aclose() (e.g. multiple instances).
        # 6. Clean up OpenRouter shared client pools
        try:
            from app.adapters.openrouter.openrouter_client import OpenRouterClient

            async with asyncio.timeout(drain_timeout):
                await OpenRouterClient.cleanup_all_clients()
        except Exception as e:
            raise_if_cancelled(e)
            logger.warning("shutdown_openrouter_cleanup_failed", exc_info=True)

        logger.info("bot_shutdown_complete")

    def _mask_path(self, path: str) -> str:
        """Mask home directory in paths for logging."""
        try:
            return str(path).replace(str(Path.home()), "~")
        except Exception:
            return path

    def _get_backup_settings(self) -> tuple[bool, int, int, str | None]:
        """Return sanitized backup configuration values."""
        runtime = getattr(self.cfg, "runtime", None)
        if runtime is None:
            return False, 0, 0, None

        enabled_raw = getattr(runtime, "db_backup_enabled", False)
        enabled = bool(enabled_raw) if isinstance(enabled_raw, bool | int) else False

        interval_raw = getattr(runtime, "db_backup_interval_minutes", 0)
        interval = interval_raw if isinstance(interval_raw, int) else 0
        interval = max(0, interval)

        retention_raw = getattr(runtime, "db_backup_retention", 0)
        retention = retention_raw if isinstance(retention_raw, int) else 0
        retention = max(retention, 0)

        backup_dir_raw = getattr(runtime, "db_backup_dir", None)
        backup_dir = (
            backup_dir_raw.strip()
            if isinstance(backup_dir_raw, str) and backup_dir_raw.strip()
            else None
        )

        return enabled, interval, retention, backup_dir

    async def _run_backup_loop(
        self, interval_minutes: int, retention: int, backup_dir: str | None
    ) -> None:
        """Periodically create database backups until cancelled.

        Implements failure tracking with alerting after consecutive failures.
        """
        if interval_minutes <= 0:
            return

        backup_directory = self._resolve_backup_dir(backup_dir)
        logger.info(
            "db_backup_loop_started",
            extra={
                "interval_minutes": interval_minutes,
                "retention": retention,
                "backup_dir": self._mask_path(str(backup_directory)),
            },
        )

        # Failure tracking
        consecutive_failures = 0
        max_consecutive_failures = 5
        last_success_time = None

        try:
            while True:
                try:
                    await self._create_database_backup(backup_directory, retention)
                    # Reset failure counter on success
                    if consecutive_failures > 0:
                        logger.info(
                            "db_backup_recovered",
                            extra={
                                "consecutive_failures": consecutive_failures,
                                "recovery_time": format_iso_z(datetime.now(UTC)),
                            },
                        )
                    consecutive_failures = 0
                    last_success_time = datetime.now(UTC)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    consecutive_failures += 1
                    logger.exception(
                        "db_backup_iteration_failed",
                        extra={
                            "error": str(exc),
                            "consecutive_failures": consecutive_failures,
                            "last_success": (
                                format_iso_z(last_success_time) if last_success_time else "never"
                            ),
                        },
                    )

                    # Alert on consecutive failures
                    if consecutive_failures >= max_consecutive_failures:
                        logger.critical(
                            "db_backup_critical_failure",
                            extra={
                                "consecutive_failures": consecutive_failures,
                                "max_failures": max_consecutive_failures,
                                "last_success": (
                                    format_iso_z(last_success_time)
                                    if last_success_time
                                    else "never"
                                ),
                                "action_required": "Manual intervention required - backups failing",
                            },
                        )
                        # Audit log for critical failures
                        with contextlib.suppress(Exception):
                            self._audit(
                                "CRITICAL",
                                "db_backup_critical_failure",
                                {
                                    "consecutive_failures": consecutive_failures,
                                    "last_success": (
                                        format_iso_z(last_success_time)
                                        if last_success_time
                                        else "never"
                                    ),
                                },
                            )

                await asyncio.sleep(interval_minutes * 60)
        except asyncio.CancelledError:
            logger.info("db_backup_loop_cancelled")
            raise

    async def _create_database_backup(self, backup_directory: Path, retention: int) -> None:
        """Create a single backup and prune according to retention settings."""
        base_name = "ratatoskr-postgres"
        suffix = ".dump"
        timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        backup_file = backup_directory / f"{base_name}-{timestamp}{suffix}"

        try:
            created_path = await asyncio.to_thread(self.db.create_backup_copy, str(backup_file))
        except FileNotFoundError as exc:
            logger.warning("db_backup_source_missing", extra={"error": str(exc)})
            return
        except ValueError as exc:
            logger.debug("db_backup_not_applicable", extra={"reason": str(exc)})
            return
        except Exception as exc:
            logger.exception("db_backup_failed", extra={"error": str(exc)})
            return

        cleanup_failed = False
        try:
            # Directory scan + stat + unlink is blocking file I/O; keep it off the loop.
            await asyncio.to_thread(
                self._cleanup_old_backups, backup_directory, base_name, suffix, retention
            )
        except Exception as exc:
            cleanup_failed = True
            logger.warning("db_backup_cleanup_failed", extra={"error": str(exc)})

        logger.info(
            "db_backup_created",
            extra={
                "backup_path": self._mask_path(str(created_path)),
                "cleanup_failed": cleanup_failed,
            },
        )

    def _resolve_backup_dir(self, override: str | None) -> Path:
        """Determine the directory to store backups in."""
        path = Path(override).expanduser() if override else Path("/data/backups")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cleanup_old_backups(
        self, backup_directory: Path, base_name: str, suffix: str, retention: int
    ) -> None:
        """Remove older backup files beyond the retention limit."""
        if retention <= 0:
            return

        try:
            candidates = sorted(
                (
                    file
                    for file in backup_directory.iterdir()
                    if file.is_file()
                    and file.name.startswith(f"{base_name}-")
                    and file.suffix == suffix
                ),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning(
                "db_backup_list_failed",
                extra={"backup_dir": self._mask_path(str(backup_directory)), "error": str(exc)},
            )
            return

        for obsolete in candidates[retention:]:
            try:
                obsolete.unlink()
            except OSError as exc:
                logger.warning(
                    "db_backup_remove_failed",
                    extra={"backup_path": self._mask_path(str(obsolete)), "error": str(exc)},
                )

    async def _run_rate_limiter_cleanup_loop(self, interval_minutes: int = 5) -> None:
        """Periodically clean up expired rate limiter entries to prevent memory leaks.

        Args:
            interval_minutes: How often to run cleanup (default: 5 minutes)
        """
        logger.info(
            "rate_limiter_cleanup_loop_started",
            extra={"interval_minutes": interval_minutes},
        )
        try:
            while True:
                await asyncio.sleep(interval_minutes * 60)
                try:
                    cleaned = await self.message_handler.message_router.cleanup_rate_limiter()
                    if cleaned > 0:
                        logger.debug(
                            "rate_limiter_cleanup_completed",
                            extra={"users_cleaned": cleaned},
                        )
                except Exception as exc:
                    logger.warning(
                        "rate_limiter_cleanup_error",
                        extra={"error": str(exc)},
                    )
                # Also clean up expired URL handler state
                try:
                    if hasattr(self.message_handler, "url_handler"):
                        url_cleaned = await self.message_handler.url_handler.cleanup_expired_state()
                        if url_cleaned > 0:
                            logger.debug(
                                "url_handler_state_cleanup_completed",
                                extra={"entries_cleaned": url_cleaned},
                            )
                except Exception as exc:
                    logger.warning(
                        "url_handler_state_cleanup_error",
                        extra={"error": str(exc)},
                    )
        except asyncio.CancelledError:
            logger.info("rate_limiter_cleanup_loop_cancelled")
            raise

    # ---- Compatibility helpers expected by tests (typed stubs) ----
    async def _safe_reply(
        self,
        message: Any,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        **extra_kwargs: Any,
    ) -> None:
        """Safely reply to a message (legacy-compatible helper)."""
        _rt = getattr(getattr(self, "cfg", None), "runtime", None)
        _timeout: float = getattr(_rt, "telegram_reply_timeout_sec", 30.0)
        try:
            if hasattr(message, "reply_text"):
                kwargs: dict[str, Any] = {}
                if parse_mode is not None:
                    kwargs["parse_mode"] = normalize_parse_mode(parse_mode)
                if reply_markup is not None:
                    kwargs["reply_markup"] = reply_markup
                if extra_kwargs:
                    kwargs.update(extra_kwargs)
                await asyncio.wait_for(message.reply_text(text, **kwargs), timeout=_timeout)
        except TimeoutError:
            logger.warning(
                "telegram_reply_timeout",
                extra={"method": "_safe_reply", "timeout_sec": _timeout},
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning(
                "safe_reply_send_failed",
                extra={
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "text_length": len(text),
                    "parse_mode": parse_mode,
                },
            )
            # Retry without parse_mode if HTML/Markdown caused the failure
            if parse_mode is not None and hasattr(message, "reply_text"):
                try:
                    retry_kwargs: dict[str, Any] = {}
                    if reply_markup is not None:
                        retry_kwargs["reply_markup"] = reply_markup
                    await asyncio.wait_for(
                        message.reply_text(text, **retry_kwargs), timeout=_timeout
                    )
                    logger.info(
                        "safe_reply_plain_text_fallback_ok",
                        extra={"text_length": len(text)},
                    )
                except Exception as retry_exc:
                    raise_if_cancelled(retry_exc)
                    logger.warning(
                        "safe_reply_plain_text_fallback_failed",
                        extra={"error": str(retry_exc)},
                    )

    async def _reply_json(
        self,
        message: Any,
        payload: dict[str, Any],
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Reply with JSON payload as a document with descriptive filename.

        Falls back to plain text if document upload fails.
        """
        _rt = getattr(getattr(self, "cfg", None), "runtime", None)
        _timeout: float = getattr(_rt, "telegram_reply_timeout_sec", 30.0)
        try:
            pretty = json.dumps(payload, ensure_ascii=False, indent=2)

            # Build a descriptive filename based on SEO keywords or TL;DR
            def _slugify(text: str, max_len: int = 60) -> str:
                import re as _re

                s = text.strip().lower()
                s = _re.sub(r"[^\w\-\s]", "", s)
                s = _re.sub(r"[\s_]+", "-", s)
                s = _re.sub(r"-+", "-", s).strip("-")
                if len(s) > max_len:
                    s = s[:max_len].rstrip("-")
                return s or "summary"

            base: str | None = None
            seo = payload.get("seo_keywords") or []
            if isinstance(seo, list) and seo:
                base = "-".join(_slugify(str(x)) for x in seo[:3] if str(x).strip())
            if not base:
                tl = str(payload.get("summary_250", "")).strip()
                if tl:
                    import re as _re

                    words = _re.findall(r"\w+", tl)[:6]
                    base = _slugify("-".join(words))
            if not base:
                base = "summary"
            ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            filename = f"{base}-{ts}.json"

            if hasattr(message, "reply_document"):
                bio = io.BytesIO(pretty.encode("utf-8"))
                bio.name = filename
                await asyncio.wait_for(
                    message.reply_document(bio, caption="📊 Full Summary JSON attached"),
                    timeout=_timeout,
                )
                return

            # Fallback to text
            if hasattr(message, "reply_text"):
                await asyncio.wait_for(
                    message.reply_text(f"```json\n{pretty}\n```"), timeout=_timeout
                )
        except TimeoutError:
            logger.warning(
                "telegram_reply_timeout",
                extra={"method": "_reply_json", "timeout_sec": _timeout},
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            try:
                text = json.dumps(payload, ensure_ascii=False)
                if hasattr(message, "reply_text"):
                    await asyncio.wait_for(message.reply_text(text), timeout=_timeout)
            except TimeoutError:
                logger.warning(
                    "telegram_reply_timeout",
                    extra={"method": "_reply_json_fallback", "timeout_sec": _timeout},
                )
            except Exception as inner_exc:
                raise_if_cancelled(inner_exc)
        _ = metadata

    async def _handle_url_flow(
        self,
        message: Any,
        url_text: str,
        *,
        correlation_id: str | None = None,
        interaction_id: int | None = None,
        silent: bool = False,
    ) -> None:
        """Process a URL message via the URL processor pipeline."""
        from app.adapters.content.url_flow_models import URLFlowRequest

        await self.url_processor.handle_url_flow(
            URLFlowRequest(
                message=message,
                url_text=url_text,
                correlation_id=correlation_id,
                interaction_id=interaction_id,
                silent=silent,
            )
        )

    async def _handle_forward_flow(
        self,
        message: Any,
        *,
        correlation_id: str | None = None,
        interaction_id: int | None = None,
    ) -> None:
        """Process a forwarded message via the forward processor pipeline."""
        cid = correlation_id or generate_correlation_id()
        await self.forward_processor.handle_forward_flow(
            message, correlation_id=cid, interaction_id=interaction_id
        )

    async def _persist_message_snapshot(self, request_id: int, message: Any) -> None:
        """Persist a Telegram message snapshot for legacy tests."""
        from app.infrastructure.persistence.message_persistence import MessagePersistence

        mp = MessagePersistence(self.db)
        await mp.persist_message_snapshot(request_id, message)

    # Behavior verified by BotSpy/_on_message /summarize coverage in tests/test_commands.py
    async def _on_message(self, message: Any) -> None:
        """Entry point used by tests; delegate to message handler."""
        uid = getattr(getattr(message, "from_user", None), "id", None)
        logger.info("handling_message uid=%s", uid, extra={"uid": uid})
        await self.message_handler.handle_message(message)

    def __setattr__(self, name: str, value: Any) -> None:
        """Re-bind reply callbacks on the response formatter when they are replaced."""
        super().__setattr__(name, value)
        if name in {"_safe_reply", "_reply_json"} and hasattr(self, "response_formatter"):
            self.response_formatter.set_reply_callbacks(
                safe_reply_func=getattr(self, "_safe_reply", None),
                reply_json_func=getattr(self, "_reply_json", None),
            )
