from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.middleware import http_red_metrics_middleware
from app.observability import metrics as facade
from app.observability.metrics_http_requests import (
    HTTP_REQUESTS_IN_FLIGHT,
    HTTP_REQUESTS_TOTAL,
)
from app.observability.metrics_status import (
    STATUS_CHECKS_TOTAL,
    STATUS_COMPONENT_STATE,
    record_status_check,
)
from app.observability.metrics_taskiq import (
    TASKIQ_EXECUTIONS_TOTAL,
    TASKIQ_IN_FLIGHT,
)
from app.tasks.middleware import TaskiqExecutionMetricsMiddleware


def _value(metric: Any, **labels: str) -> float:
    if metric is None:
        pytest.skip("prometheus_client metrics are not initialized")
    return float(metric.labels(**labels)._value.get())


def test_http_red_uses_route_template_and_balances_in_flight() -> None:
    test_app = FastAPI()
    test_app.middleware("http")(http_red_metrics_middleware)

    @test_app.get("/metrics-test/items/{item_id}")
    async def _item(item_id: str) -> dict[str, str]:
        return {"id": item_id}

    route = "/metrics-test/items/{item_id}"
    before = _value(
        HTTP_REQUESTS_TOTAL,
        route=route,
        method="GET",
        status_class="2xx",
    )
    in_flight_before = _value(HTTP_REQUESTS_IN_FLIGHT, method="GET")

    response = TestClient(test_app).get("/metrics-test/items/sensitive-item-id")

    assert response.status_code == 200
    assert (
        _value(
            HTTP_REQUESTS_TOTAL,
            route=route,
            method="GET",
            status_class="2xx",
        )
        == before + 1
    )
    assert _value(HTTP_REQUESTS_IN_FLIGHT, method="GET") == in_flight_before
    assert b"sensitive-item-id" not in facade.get_metrics()


def test_http_red_records_unhandled_exception_as_5xx() -> None:
    test_app = FastAPI()
    test_app.middleware("http")(http_red_metrics_middleware)

    @test_app.get("/metrics-test/failure")
    async def _failure() -> None:
        raise RuntimeError("private failure")

    before = _value(
        HTTP_REQUESTS_TOTAL,
        route="/metrics-test/failure",
        method="GET",
        status_class="5xx",
    )

    response = TestClient(test_app, raise_server_exceptions=False).get(
        "/metrics-test/failure"
    )

    assert response.status_code == 500
    assert (
        _value(
            HTTP_REQUESTS_TOTAL,
            route="/metrics-test/failure",
            method="GET",
            status_class="5xx",
        )
        == before + 1
    )


def test_status_metrics_have_bounded_component_and_state_labels() -> None:
    before = _value(
        STATUS_CHECKS_TOTAL,
        component="other",
        status="unknown",
    )

    record_status_check("private-component-name", "invalid-state", 0.1)

    assert (
        _value(
            STATUS_CHECKS_TOTAL,
            component="other",
            status="unknown",
        )
        == before + 1
    )
    assert _value(STATUS_COMPONENT_STATE, component="other") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("is_err", [False, True])
async def test_taskiq_red_tracks_outcome_and_balances_in_flight(is_err: bool) -> None:
    middleware = TaskiqExecutionMetricsMiddleware()
    message = SimpleNamespace(
        task_name="ratatoskr.url.process",
        task_id=f"red-test-{is_err}",
    )
    result = SimpleNamespace(is_err=is_err, execution_time=0.01)
    outcome = "error" if is_err else "success"
    before = _value(
        TASKIQ_EXECUTIONS_TOTAL,
        task="ratatoskr.url.process",
        outcome=outcome,
    )
    in_flight_before = _value(TASKIQ_IN_FLIGHT, task="ratatoskr.url.process")

    await middleware.pre_execute(message)
    assert _value(TASKIQ_IN_FLIGHT, task="ratatoskr.url.process") == in_flight_before + 1
    await middleware.post_execute(message, result)

    assert (
        _value(
            TASKIQ_EXECUTIONS_TOTAL,
            task="ratatoskr.url.process",
            outcome=outcome,
        )
        == before + 1
    )
    assert _value(TASKIQ_IN_FLIGHT, task="ratatoskr.url.process") == in_flight_before


def test_red_metrics_are_reexported_by_facade() -> None:
    assert facade.HTTP_REQUESTS_TOTAL is HTTP_REQUESTS_TOTAL
    assert facade.STATUS_CHECKS_TOTAL is STATUS_CHECKS_TOTAL
    assert facade.TASKIQ_EXECUTIONS_TOTAL is TASKIQ_EXECUTIONS_TOTAL
