"""Background request processor for Mobile API."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from app.api.background import (
    BackgroundDbOverrideFactory,
    BackgroundFailureHandler,
    BackgroundLockManager,
    BackgroundProgressPublisher,
    BackgroundRequestExecutor,
    BackgroundRetryRunner,
    ForwardBackgroundRequestHandler,
    RetryPolicy,
    StageError,
    UrlBackgroundRequestHandler,
)
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Awaitable, Callable

    from app.application.ports.requests import RequestRepositoryFactory
    from app.application.ports.summaries import SummaryRepositoryFactory
    from app.config import AppConfig
    from app.db.session import Database

logger = get_logger(__name__)


class _NullRepository:
    async def async_get_request_by_id(self, _request_id: int) -> dict[str, Any] | None:
        return None

    async def async_update_request_status_with_correlation(
        self, _request_id: int, _status: str, _correlation_id: str | None
    ) -> None:
        return None

    async def async_get_summary_by_request(self, _request_id: int) -> dict[str, Any] | None:
        return None

    async def async_upsert_summary(self, **_kwargs: Any) -> None:
        return None


class BackgroundProcessor:
    """Process background requests with explicit collaborators."""

    def __init__(
        self,
        *,
        cfg: AppConfig,
        db: Database,
        url_processor: Any,
        redis: Any | None,
        semaphore: asyncio.Semaphore,
        audit_func: Callable[[str, str, dict[str, Any]], None],
        url_processor_factory: Callable[[Database], Any] | None = None,
        database_builder: Callable[[AppConfig], Database] | None = None,
        request_repo: Any | None = None,
        summary_repo: Any | None = None,
        request_repo_factory: RequestRepositoryFactory | None = None,
        summary_repo_factory: SummaryRepositoryFactory | None = None,
        progress_event_repo: Any | None = None,
        deps: Any | None = None,
    ) -> None:
        self.cfg = cfg
        self.db = db
        self.url_processor = url_processor
        self.redis = redis
        self._sem = semaphore
        self._audit = audit_func
        self._processing_tasks: set[asyncio.Task[None]] = set()
        self.request_repo = request_repo or _NullRepository()
        self.summary_repo = summary_repo or _NullRepository()
        self._request_repo_factory = request_repo_factory
        self._summary_repo_factory = summary_repo_factory

        self._retry = RetryPolicy(
            attempts=cfg.background.retry_attempts,
            base_delay_ms=cfg.background.retry_base_delay_ms,
            max_delay_ms=cfg.background.retry_max_delay_ms,
            jitter_ratio=cfg.background.retry_jitter_ratio,
        )

        if deps is not None:
            self._db_override_factory = deps.db_override_factory
            self._lock_manager = deps.lock_manager
            self._retry_runner = deps.retry_runner
            self._progress_publisher = deps.progress_publisher
            self._failure_handler = deps.failure_handler
            self._url_request_handler = deps.url_handler
            self._forward_request_handler = deps.forward_handler
        else:
            self._db_override_factory = BackgroundDbOverrideFactory(
                cfg=cfg,
                default_db=db,
                default_url_processor=url_processor,
                database_builder=database_builder,
                url_processor_factory=url_processor_factory,
            )
            self._lock_manager = BackgroundLockManager(cfg=cfg, redis=redis, logger=logger)
            self._retry_runner = BackgroundRetryRunner(policy=self._retry, logger=logger)
            self._progress_publisher = BackgroundProgressPublisher(
                redis=redis,
                logger=logger,
                progress_event_repo=progress_event_repo,
            )
            self._url_request_handler = UrlBackgroundRequestHandler(
                cfg=cfg,
                publish_update=self._progress_publisher.publish,
                run_stage=self._run_stage,
                summary_repo_for_db=self._get_summary_repo_for_db,
            )
            self._forward_request_handler = ForwardBackgroundRequestHandler(
                cfg=cfg,
                publish_update=self._progress_publisher.publish,
                run_stage=self._run_stage,
                summary_repo_for_db=self._get_summary_repo_for_db,
            )
            self._failure_handler = BackgroundFailureHandler(
                logger=logger,
                retry_policy=self._retry,
                request_repo_for_db=self._get_request_repo_for_db,
                mark_status=self._mark_status,
                progress_publisher=self._progress_publisher,
            )
        self._local_locks = getattr(self._lock_manager, "_local_locks", {})

        self._executor = BackgroundRequestExecutor(
            logger=logger,
            db_override_factory=self._db_override_factory,
            lock_manager=self._lock_manager,
            request_repo_for_db=self._get_request_repo_for_db,
            has_existing_summary=self._has_existing_summary,
            mark_status=self._mark_status,
            progress_publisher=self._progress_publisher,
            url_handler=self._url_request_handler,
            forward_handler=self._forward_request_handler,
            failure_handler=self._failure_handler,
            error_payload_builder=self._build_error_payload,
        )

    async def execute_request(
        self,
        request_id: int,
        *,
        correlation_id: str | None = None,
        db_path: str | None = None,
    ) -> None:
        await self._executor.execute(
            request_id=request_id,
            correlation_id=correlation_id,
            db_path=db_path,
        )

    def _get_request_repo_for_db(self, db: Database) -> Any:
        if db == self.db or self._request_repo_factory is None:
            return self.request_repo
        return self._request_repo_factory(db)

    def _get_summary_repo_for_db(self, db: Database) -> Any:
        if db == self.db or self._summary_repo_factory is None:
            return self.summary_repo
        return self._summary_repo_factory(db)

    def _maybe_override_db(self, db_path: str | None) -> tuple[Database, Any]:
        return cast("tuple[Database, Any]", self._db_override_factory.resolve(db_path))

    async def _release_lock(self, handle: Any) -> None:
        await self._lock_manager.release(handle)

    async def _run_stage(
        self,
        stage: str,
        correlation_id: str,
        func: Callable[[], Awaitable[Any]],
    ) -> Any:
        try:
            return await self._run_with_backoff(func, stage, correlation_id)
        except Exception as exc:
            raise StageError(stage, exc) from exc

    async def _run_with_backoff(
        self,
        func: Callable[[], Awaitable[Any]],
        stage: str,
        correlation_id: str,
    ) -> Any:
        return await self._retry_runner.run_with_backoff(func, stage, correlation_id)

    async def _has_existing_summary(self, db: Database, request_id: int) -> bool:
        repo = self._get_summary_repo_for_db(db)
        try:
            return bool(await repo.async_get_summary_by_request(request_id))
        except Exception as exc:
            logger.debug(
                "bg_summary_check_failed",
                extra={
                    "request_id": request_id,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return False

    async def _mark_status(
        self, db: Database, request_id: int, status: str, correlation_id: str | None
    ) -> None:
        repo = self._get_request_repo_for_db(db)
        try:
            await repo.async_update_request_status_with_correlation(
                request_id,
                status,
                correlation_id,
            )
        except Exception as exc:
            logger.warning(
                "bg_request_status_save_failed",
                exc_info=True,
                extra={"request_id": request_id, "status": status, "error": str(exc)},
            )

    async def _publish_update(
        self,
        request_id: int,
        status: str,
        stage: str,
        message: str,
        progress: float,
        error: str | None = None,
    ) -> None:
        await self._progress_publisher.publish(
            request_id,
            status,
            stage,
            message,
            progress,
            error,
        )

    def _resolve_request_language(
        self,
        request: dict[str, Any],
        content_text: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        return cast(
            "str",
            self._url_request_handler.resolve_request_language(
                request,
                content_text,
                metadata=metadata,
            ),
        )

    @staticmethod
    def _build_error_payload(stage: str, exc: Exception) -> dict[str, Any]:
        code_map = {
            "extraction": "EXTRACTION_FAILED",
            "summarization": "LLM_FAILED",
            "validation": "VALIDATION_FAILED",
            "lock": "LOCK_FAILED",
        }
        return {
            "error_type": exc.__class__.__name__,
            "error_code": code_map.get(stage, "UNKNOWN_ERROR"),
            "error_message": str(exc),
            "error_stage": stage,
        }
