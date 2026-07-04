"""Taskiq middleware for scheduled-task observability.

ChronicFailureMiddleware ports the consecutive-failure tracking from
SchedulerService._job_consecutive_failures to the taskiq middleware layer,
preserving the existing record_scheduler_chronic_failure Prometheus metric.
"""

from __future__ import annotations

import re
import traceback
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from taskiq import TaskiqMiddleware

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from taskiq.message import TaskiqMessage

from app.core.logging_utils import get_logger, redact_for_logging
from app.observability.attributes import REQUEST_CORRELATION_ID, TASK_IS_ERR
from app.observability.metrics import record_scheduler_chronic_failure, record_taskiq_retry_outcome

logger = get_logger(__name__)

_CHRONIC_FAILURE_THRESHOLD = 3

# Dead-letter persistence writes raw task args/kwargs to `taskiq_failed_jobs` for
# replay/debugging. Any kwarg whose name looks secret must never land there in
# plaintext, even though no current task takes a literal secret kwarg -- this
# guards against that becoming true in the future without anyone noticing.
_DEAD_LETTER_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|passwd|api[-_]?key|authorization|cookie|credential|"
    r"(?:^|[-_])key(?:$|[-_]))",
    re.IGNORECASE,
)
_DEAD_LETTER_MAX_VALUE_CHARS = 2000
_DEAD_LETTER_REDACTED = "***REDACTED***"


def _redact_dead_letter_payload(value: Any, *, key: str | None = None) -> Any:
    """Redact secret-looking kwargs and cap oversized values before dead-letter persistence."""
    if key is not None and _DEAD_LETTER_SECRET_KEY_RE.search(key):
        return _DEAD_LETTER_REDACTED
    if isinstance(value, dict):
        return {
            str(item_key): _redact_dead_letter_payload(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_dead_letter_payload(item) for item in value]
    redacted = redact_for_logging(value, key=key)
    if isinstance(redacted, str) and len(redacted) > _DEAD_LETTER_MAX_VALUE_CHARS:
        return redacted[:_DEAD_LETTER_MAX_VALUE_CHARS] + "... [truncated]"
    return redacted


class ChronicFailureMiddleware(TaskiqMiddleware):
    """Track consecutive task failures and emit a Prometheus metric at threshold."""

    def __init__(self) -> None:
        self._consecutive_failures: dict[str, int] = defaultdict(int)

    async def post_execute(
        self,
        message: TaskiqMessage,
        result: Any,
    ) -> Any:
        task_name = message.task_name
        if result.is_err:
            count = self._consecutive_failures[task_name] + 1
            self._consecutive_failures[task_name] = count
            if count >= _CHRONIC_FAILURE_THRESHOLD:
                logger.error(
                    "scheduler_job_chronic_failure",
                    extra={
                        "task_name": task_name,
                        "consecutive": count,
                        "error": repr(result.error),
                    },
                )
                record_scheduler_chronic_failure(task_name)
        elif self._consecutive_failures.get(task_name, 0) > 0:
            logger.info("scheduler_job_recovered", extra={"task_name": task_name})
            self._consecutive_failures[task_name] = 0
        return result


class TaskiqDeadLetterMiddleware(TaskiqMiddleware):
    """Persist terminal Taskiq failures and record retry outcome metrics."""

    def __init__(
        self,
        persist_failed_job: Callable[..., Awaitable[int]] | None = None,
    ) -> None:
        self._persist_failed_job = persist_failed_job
        self._database: Any | None = None

    async def shutdown(self) -> None:
        if self._database is not None:
            await self._database.dispose()
            self._database = None

    async def on_error(
        self,
        message: TaskiqMessage,
        result: Any,
        exception: BaseException,
    ) -> None:
        labels = dict(message.labels or {})
        retry_on_error = _coerce_bool(labels.get("retry_on_error"))
        current_retries = _coerce_int(labels.get("_retries"), default=0)
        next_attempt = current_retries + 1
        max_retries = _coerce_int(labels.get("max_retries"), default=0)

        if retry_on_error and next_attempt < max_retries:
            record_taskiq_retry_outcome(task=message.task_name, outcome="retry")
            return

        traceback_text = "".join(
            traceback.format_exception(type(exception), exception, exception.__traceback__)
        )
        error_text = f"{type(exception).__name__}: {exception!s}"[:2000]

        try:
            failed_job_id = await self._persist_terminal_failure(
                task_name=message.task_name,
                task_id=message.task_id,
                args=_redact_dead_letter_payload(list(message.args or [])),
                kwargs=_redact_dead_letter_payload(dict(message.kwargs or {})),
                labels=labels,
                traceback_text=traceback_text,
                error_text=error_text,
                attempt_count=max(1, next_attempt),
            )
        except Exception as persist_exc:
            logger.exception(
                "taskiq_dead_letter_persist_failed",
                extra={
                    "task_name": message.task_name,
                    "task_id": message.task_id,
                    "error": str(persist_exc),
                },
            )
            return

        record_taskiq_retry_outcome(task=message.task_name, outcome="dead_letter")
        logger.error(
            "taskiq_dead_lettered",
            extra={
                "task_name": message.task_name,
                "task_id": message.task_id,
                "failed_job_id": failed_job_id,
                "attempt_count": max(1, next_attempt),
                "error": error_text,
            },
        )

    async def post_execute(
        self,
        message: TaskiqMessage,
        result: Any,
    ) -> Any:
        labels = dict(message.labels or {})
        if not getattr(result, "is_err", False) and _coerce_int(labels.get("_retries"), default=0):
            record_taskiq_retry_outcome(task=message.task_name, outcome="success_after_retry")
        return result

    async def _persist_terminal_failure(
        self,
        *,
        task_name: str,
        task_id: str | None,
        args: list[Any],
        kwargs: dict[str, Any],
        labels: dict[str, Any],
        traceback_text: str,
        error_text: str,
        attempt_count: int,
    ) -> int:
        if self._persist_failed_job is not None:
            return await self._persist_failed_job(
                task_name=task_name,
                task_id=task_id,
                args=args,
                kwargs=kwargs,
                labels=labels,
                traceback_text=traceback_text,
                error_text=error_text,
                attempt_count=attempt_count,
            )

        from app.config import load_config
        from app.db.session import Database
        from app.infrastructure.persistence.repositories.taskiq_failed_job_repository import (
            TaskiqFailedJobRepository,
        )

        if self._database is None:
            cfg = load_config(allow_stub_telegram=True)
            self._database = Database(cfg.database)
        repo = TaskiqFailedJobRepository(self._database)
        return await repo.async_insert_failed_job(
            task_name=task_name,
            task_id=task_id,
            args=args,
            kwargs=kwargs,
            labels=labels,
            traceback_text=traceback_text,
            error_text=error_text,
            attempt_count=attempt_count,
        )


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class OTelPropagationMiddleware(TaskiqMiddleware):
    """Propagate W3C trace context across the taskiq broker hop.

    Producer side: injects current span context into message.labels so the
    trace follows the task through the Redis stream.
    Worker side: extracts the injected context and starts a child span.
    """

    async def pre_send(self, message: TaskiqMessage) -> TaskiqMessage:
        try:
            from opentelemetry.propagate import inject

            inject(message.labels)
        except Exception as exc:
            logger.debug("otel_pre_send_failed", exc_info=exc)
        return message

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        try:
            from opentelemetry import trace
            from opentelemetry.propagate import extract

            ctx = extract(message.labels)
            tracer = trace.get_tracer(__name__)
            span = tracer.start_span(
                f"taskiq.{message.task_name}",
                context=ctx,
                attributes={
                    "taskiq.task_name": message.task_name,
                    "taskiq.task_id": message.task_id,
                },
            )
            object.__setattr__(message, "_otel_span", span)
            cid = (message.kwargs or {}).get("correlation_id") or (
                (message.labels or {}).get("correlation_id")
            )
            if cid:
                span.set_attribute(REQUEST_CORRELATION_ID, cid)
        except Exception as exc:
            logger.debug("otel_pre_execute_failed", exc_info=exc)
        return message

    async def post_execute(self, message: TaskiqMessage, result: Any) -> Any:
        try:
            span = getattr(message, "_otel_span", None)
            if span is not None:
                if hasattr(result, "is_err"):
                    span.set_attribute(TASK_IS_ERR, result.is_err)
                span.end()
        except Exception as exc:
            logger.debug("otel_post_execute_failed", exc_info=exc)
        return result
