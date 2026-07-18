"""Taskiq broker — RedisStreamBroker for durable scheduled-task delivery.

URL resolution order:
  1. TASKIQ_BROKER_REDIS_URL  (explicit override)
  2. REDIS_URL                (shared Redis URL)
  3. redis://{REDIS_HOST}:{REDIS_PORT}/{TASKIQ_BROKER_REDIS_DB}  (component fallback)

Set TASKIQ_BROKER=memory for local dev / tests without Redis.
"""

from __future__ import annotations

import os
from typing import Any

from app.tasks.middleware import (
    ChronicFailureMiddleware,
    OTelPropagationMiddleware,
    TaskiqDeadLetterMiddleware,
    TaskiqExecutionMetricsMiddleware,
)

_simple_retry_middleware_cls: Any | None
try:
    from taskiq import SimpleRetryMiddleware
except (ImportError, AttributeError):  # pragma: no cover - compatibility for test stubs
    _simple_retry_middleware_cls = None
else:
    _simple_retry_middleware_cls = SimpleRetryMiddleware

# Initialise OTel tracing before broker/redis clients are constructed.
try:
    from app.observability.otel import init_tracing as _init_tracing

    _init_tracing()  # reads OTEL_ENABLED / OTEL_EXPORTER_OTLP_ENDPOINT from env
except Exception:  # pragma: no cover
    pass

_broker_type = os.getenv("TASKIQ_BROKER", "redis").lower()
_middlewares = [
    *(  # SimpleRetryMiddleware may be absent in lightweight taskiq test stubs.
        [_simple_retry_middleware_cls(default_retry_count=3, default_retry_label=False)]
        if _simple_retry_middleware_cls is not None
        else []
    ),
    TaskiqExecutionMetricsMiddleware(),
    ChronicFailureMiddleware(),
    TaskiqDeadLetterMiddleware(),
    OTelPropagationMiddleware(),
]

if _broker_type == "memory":
    from taskiq import AsyncBroker, InMemoryBroker

    broker: AsyncBroker = InMemoryBroker().with_middlewares(*_middlewares)
else:
    from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

    _url = (
        os.getenv("TASKIQ_BROKER_REDIS_URL")
        or os.getenv("REDIS_URL")
        or "redis://{}:{}/{}".format(
            os.getenv("REDIS_HOST", "127.0.0.1"),
            os.getenv("REDIS_PORT", "6379"),
            os.getenv("TASKIQ_BROKER_REDIS_DB", "2"),
        )
    )
    _result_ttl = int(os.getenv("TASKIQ_RESULT_TTL_SEC", "3600"))

    _result_backend: RedisAsyncResultBackend[Any] = RedisAsyncResultBackend(
        redis_url=_url,
        result_ex_time=_result_ttl,
    )
    broker = (
        RedisStreamBroker(url=_url)
        .with_result_backend(_result_backend)
        .with_middlewares(*_middlewares)
    )
