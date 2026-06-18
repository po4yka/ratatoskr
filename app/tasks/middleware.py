"""Taskiq middleware for scheduled-task observability.

ChronicFailureMiddleware ports the consecutive-failure tracking from
SchedulerService._job_consecutive_failures to the taskiq middleware layer,
preserving the existing record_scheduler_chronic_failure Prometheus metric.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from taskiq import TaskiqMiddleware

if TYPE_CHECKING:
    from taskiq.message import TaskiqMessage

from app.core.logging_utils import get_logger
from app.observability.attributes import REQUEST_CORRELATION_ID, TASK_IS_ERR
from app.observability.metrics import record_scheduler_chronic_failure

logger = get_logger(__name__)

_CHRONIC_FAILURE_THRESHOLD = 3


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
