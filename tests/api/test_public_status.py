from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.api.models.responses.status import (
    PublicStatusComponent,
    PublicStatusGroup,
    PublicStatusLevel,
    PublicStatusResponse,
    PublicStatusSummary,
)
from app.api.services.status_service import (
    PublicStatusService,
    clear_status_cache,
    get_public_status_service,
)
from app.config.deployment import DeploymentConfig
from app.core.time_utils import UTC
from app.db.models.ai_backup import AiBackupStatus
from app.db.models.git_backup import GitMirrorStatus

_COMPONENT_IDS = (
    "api",
    "web_application",
    "telegram_bot",
    "postgresql",
    "redis",
    "vector_search",
    "extraction",
    "ai_summarization",
    "taskiq_worker",
    "scheduler",
    "vector_reconciliation",
    "postgresql_backup",
    "github_repository_backups",
    "chatgpt_backup",
    "claude_backup",
)


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterable[None]:
    clear_status_cache()
    yield
    clear_status_cache()


def _probe(value: PublicStatusLevel | dict[str, Any]):
    async def _run() -> PublicStatusLevel | dict[str, Any]:
        return value

    return _run


def _probes(default: PublicStatusLevel = PublicStatusLevel.OPERATIONAL):
    return {component_id: _probe(default) for component_id in _COMPONENT_IDS}


def _components(result: PublicStatusResponse) -> dict[str, PublicStatusComponent]:
    return {component.id: component for group in result.groups for component in group.components}


@pytest.mark.asyncio
async def test_status_aggregation_and_exact_summary() -> None:
    probes = _probes()
    probes["redis"] = _probe(PublicStatusLevel.DISABLED)
    probes["vector_search"] = _probe(PublicStatusLevel.OUTAGE)
    service = PublicStatusService(
        deployment=DeploymentConfig(), component_probes=probes, cache_enabled=False
    )

    result = await service.get_status()

    assert result.status is PublicStatusLevel.DEGRADED
    assert result.summary.model_dump() == {
        "total": 15,
        "operational": 13,
        "degraded": 0,
        "outage": 1,
        "unknown": 0,
        "disabled": 1,
    }
    assert sum(result.summary.model_dump()[level.value] for level in PublicStatusLevel) == 15


@pytest.mark.asyncio
async def test_all_healthy_signals_are_operational() -> None:
    service = PublicStatusService(
        deployment=DeploymentConfig(),
        component_probes=_probes(),
        cache_enabled=False,
    )

    result = await service.get_status()

    assert result.status is PublicStatusLevel.OPERATIONAL
    assert result.summary.operational == result.summary.total == 15


@pytest.mark.parametrize(
    ("states", "age", "expected"),
    [
        (("closed",), timedelta(minutes=1), PublicStatusLevel.OPERATIONAL),
        (("half_open",), timedelta(minutes=1), PublicStatusLevel.DEGRADED),
        (("open",), timedelta(minutes=1), PublicStatusLevel.OUTAGE),
        (("closed", "open"), timedelta(minutes=1), PublicStatusLevel.DEGRADED),
        (("closed",), timedelta(hours=25), PublicStatusLevel.UNKNOWN),
        ((), timedelta(minutes=1), PublicStatusLevel.UNKNOWN),
    ],
)
def test_ai_status_uses_only_fresh_openrouter_circuit_updates(
    states: tuple[str, ...], age: timedelta, expected: PublicStatusLevel
) -> None:
    now = datetime.now(UTC)
    sample = "\n".join(
        "openrouter_circuit_breaker_last_update_timestamp_seconds"
        f'{{model="model-{index}",state="{state}"}} {(now - age).timestamp()}'
        for index, state in enumerate(states)
    ).encode()

    assert PublicStatusService._parse_openrouter_status(sample, now=now) is expected


def test_ai_status_prefers_latest_state_per_model() -> None:
    now = datetime.now(UTC)
    old_open = (now - timedelta(minutes=10)).timestamp()
    recent_closed = (now - timedelta(minutes=1)).timestamp()
    payload = (
        "openrouter_circuit_breaker_last_update_timestamp_seconds"
        f'{{model="primary",state="open"}} {old_open}\n'
        "openrouter_circuit_breaker_last_update_timestamp_seconds"
        f'{{model="primary",state="closed"}} {recent_closed}\n'
    ).encode()

    assert (
        PublicStatusService._parse_openrouter_status(payload, now=now)
        is PublicStatusLevel.OPERATIONAL
    )


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(hours=36), PublicStatusLevel.OPERATIONAL),
        (timedelta(hours=36, seconds=1), PublicStatusLevel.DEGRADED),
        (timedelta(hours=48), PublicStatusLevel.DEGRADED),
        (timedelta(hours=48, seconds=1), PublicStatusLevel.OUTAGE),
    ],
)
def test_postgresql_backup_status_has_explicit_freshness_boundaries(
    age: timedelta, expected: PublicStatusLevel
) -> None:
    now = datetime.now(UTC)
    payload = (
        f"ratatoskr_pg_backup_last_success_timestamp_seconds {(now - age).timestamp()}\n"
    ).encode()

    signal = PublicStatusService._parse_postgresql_backup_status(payload, now=now)

    assert signal.level is expected


def test_backup_status_reports_partial_git_coverage_without_sensitive_details() -> None:
    now = datetime.now(UTC)
    rows = [
        SimpleNamespace(status=GitMirrorStatus.OK, last_mirrored_at=now),
        SimpleNamespace(status=GitMirrorStatus.FAILED, last_mirrored_at=None),
        SimpleNamespace(status=GitMirrorStatus.EXCLUDED, last_mirrored_at=None),
    ]

    signal = PublicStatusService._github_backup_status(rows, now=now)

    assert signal.level is PublicStatusLevel.DEGRADED
    assert signal.message == "Repository backup coverage is partial"


def test_ai_backup_status_exposes_only_coarse_authorization_action() -> None:
    row = SimpleNamespace(
        status=AiBackupStatus.AUTH_EXPIRED,
        last_backed_up_at=None,
        last_error="cookie secret for private account",
    )

    signal = PublicStatusService._ai_backup_status([row])

    assert signal.level is PublicStatusLevel.OUTAGE
    assert signal.message == "Authorization required"
    assert "cookie" not in signal.message
    assert "private" not in signal.message


def test_vector_reconciliation_status_uses_run_and_lag_metrics() -> None:
    healthy = (
        b'ratatoskr_vector_reconcile_runs_total{status="success"} 2\n'
        b"ratatoskr_vector_reconcile_oldest_lag_seconds 10\n"
    )
    behind = healthy.replace(b" 10\n", b" 3601\n")
    failing = b'ratatoskr_vector_reconcile_runs_total{status="error"} 1\n'

    assert (
        PublicStatusService._parse_vector_reconciliation_status(healthy).level
        is PublicStatusLevel.OPERATIONAL
    )
    assert (
        PublicStatusService._parse_vector_reconciliation_status(behind).level
        is PublicStatusLevel.DEGRADED
    )
    assert (
        PublicStatusService._parse_vector_reconciliation_status(failing).level
        is PublicStatusLevel.OUTAGE
    )


@pytest.mark.parametrize(
    ("success_age", "failure_age", "expected"),
    [
        (timedelta(minutes=1), None, PublicStatusLevel.OPERATIONAL),
        (timedelta(minutes=5), timedelta(minutes=1), PublicStatusLevel.DEGRADED),
        (timedelta(hours=25), None, PublicStatusLevel.UNKNOWN),
        (None, None, PublicStatusLevel.UNKNOWN),
    ],
)
def test_extraction_status_uses_fresh_runtime_chain_results(
    success_age: timedelta | None,
    failure_age: timedelta | None,
    expected: PublicStatusLevel,
) -> None:
    now = datetime.now(UTC)
    samples: list[str] = []
    if success_age is not None:
        samples.append(
            "ratatoskr_scraper_chain_last_result_timestamp_seconds"
            f'{{outcome="success"}} {(now - success_age).timestamp()}'
        )
    if failure_age is not None:
        samples.append(
            "ratatoskr_scraper_chain_last_result_timestamp_seconds"
            f'{{outcome="failure"}} {(now - failure_age).timestamp()}'
        )

    signal = PublicStatusService._parse_extraction_status("\n".join(samples).encode(), now=now)

    assert signal.level is expected


@pytest.mark.asyncio
async def test_bot_and_extraction_share_one_metrics_scrape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = _probes()
    probes.pop("telegram_bot")
    probes.pop("extraction")
    now = datetime.now(UTC).timestamp()
    service = PublicStatusService(
        deployment=DeploymentConfig(STATUS_BOT_METRICS_URL="http://bot:9101/metrics"),
        component_probes=probes,
        cache_enabled=False,
    )
    calls = 0

    async def _fetch(_url: str | None) -> tuple[PublicStatusLevel, bytes | None]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return (
            PublicStatusLevel.OPERATIONAL,
            (
                "ratatoskr_scraper_chain_last_result_timestamp_seconds"
                f'{{outcome="success"}} {now}\n'
            ).encode(),
        )

    monkeypatch.setattr(service, "_fetch_metrics", _fetch)

    result = await service.get_status()

    assert _components(result)["extraction"].status is PublicStatusLevel.OPERATIONAL
    assert calls == 1


@pytest.mark.asyncio
async def test_worker_and_ai_share_one_metrics_scrape(monkeypatch: pytest.MonkeyPatch) -> None:
    probes = _probes()
    probes.pop("ai_summarization")
    probes.pop("taskiq_worker")
    service = PublicStatusService(
        deployment=DeploymentConfig(STATUS_WORKER_METRICS_URL="http://worker:9102/metrics"),
        component_probes=probes,
        cache_enabled=False,
    )
    calls = 0

    async def _fetch(_url: str | None) -> tuple[PublicStatusLevel, bytes | None]:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return (
            PublicStatusLevel.OPERATIONAL,
            (
                "openrouter_circuit_breaker_last_update_timestamp_seconds"
                f'{{model="primary",state="closed"}} {datetime.now(UTC).timestamp()}\n'
            ).encode(),
        )

    monkeypatch.setattr(service, "_fetch_metrics", _fetch)

    result = await service.get_status()

    assert result.status is PublicStatusLevel.OPERATIONAL
    assert calls == 1


@pytest.mark.asyncio
async def test_unknown_degrades_overall_and_critical_outage_causes_outage() -> None:
    probes = _probes()
    probes["ai_summarization"] = _probe(PublicStatusLevel.UNKNOWN)
    service = PublicStatusService(
        deployment=DeploymentConfig(), component_probes=probes, cache_enabled=False
    )
    assert (await service.get_status()).status is PublicStatusLevel.DEGRADED

    probes["postgresql"] = _probe(PublicStatusLevel.OUTAGE)
    service = PublicStatusService(
        deployment=DeploymentConfig(), component_probes=probes, cache_enabled=False
    )
    assert (await service.get_status()).status is PublicStatusLevel.OUTAGE


@pytest.mark.asyncio
async def test_unconfigured_process_is_unknown_and_unreachable_process_is_outage() -> None:
    service = PublicStatusService(deployment=DeploymentConfig(), cache_enabled=False)
    assert await service._probe_process(None) is PublicStatusLevel.UNKNOWN

    unreachable = PublicStatusService(
        deployment=DeploymentConfig(
            STATUS_BOT_METRICS_URL="http://127.0.0.1:1/metrics",
            STATUS_PROBE_TIMEOUT_SECONDS=0.2,
            STATUS_TOTAL_TIMEOUT_SECONDS=1,
        ),
        cache_enabled=False,
    )
    assert (
        await unreachable._probe_process("http://127.0.0.1:1/metrics") is PublicStatusLevel.OUTAGE
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, PublicStatusLevel.OPERATIONAL),
        (503, PublicStatusLevel.OUTAGE),
    ],
)
async def test_qdrant_status_uses_live_ready_endpoint(
    respx_mock, status_code: int, expected: PublicStatusLevel
) -> None:
    url = "http://qdrant:6333/readyz"
    route = respx_mock.get(url).mock(return_value=httpx.Response(status_code))
    service = PublicStatusService(
        deployment=DeploymentConfig(STATUS_QDRANT_READY_URL=url),
        cache_enabled=False,
    )

    assert await service._probe_http_ready(url) is expected
    assert route.called


@pytest.mark.asyncio
async def test_unconfigured_qdrant_readiness_is_unknown() -> None:
    service = PublicStatusService(deployment=DeploymentConfig(), cache_enabled=False)

    assert await service._probe_http_ready(None) is PublicStatusLevel.UNKNOWN


@pytest.mark.asyncio
async def test_slow_probe_is_bounded_and_reported_as_outage() -> None:
    async def _slow() -> PublicStatusLevel:
        await asyncio.sleep(1)
        return PublicStatusLevel.OPERATIONAL

    probes = _probes()
    probes["telegram_bot"] = _slow
    service = PublicStatusService(
        deployment=DeploymentConfig(
            STATUS_PROBE_TIMEOUT_SECONDS=0.05,
            STATUS_TOTAL_TIMEOUT_SECONDS=0.1,
        ),
        component_probes=probes,
        cache_enabled=False,
    )
    loop = asyncio.get_running_loop()
    started = loop.time()

    result = await service.get_status()

    assert loop.time() - started < 0.5
    assert _components(result)["telegram_bot"].status is PublicStatusLevel.OUTAGE


@pytest.mark.asyncio
async def test_probe_failure_log_contains_only_safe_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "private-token-and-hostname"

    async def _fails() -> PublicStatusLevel:
        raise RuntimeError(secret)

    probes = _probes()
    probes["postgresql"] = _fails
    service = PublicStatusService(
        deployment=DeploymentConfig(),
        component_probes=probes,
        cache_enabled=False,
    )

    with caplog.at_level(logging.WARNING, logger="app.api.services.status_service"):
        result = await service.get_status()

    record = next(item for item in caplog.records if item.message == "public_status_probe_failed")
    rendered = record.getMessage() + str(record.__dict__)
    assert _components(result)["postgresql"].status is PublicStatusLevel.OUTAGE
    assert getattr(record, "component", None) == "postgresql"
    assert getattr(record, "error_type", None) == "RuntimeError"
    assert secret not in rendered
    assert not hasattr(record, "error")


@pytest.mark.asyncio
async def test_cancellation_resistant_probe_cannot_extend_total_timeout() -> None:
    release = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def _resistant() -> PublicStatusLevel:
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                asyncio.current_task().uncancel()
        return PublicStatusLevel.OPERATIONAL

    probes = _probes()
    probes["telegram_bot"] = _resistant
    service = PublicStatusService(
        deployment=DeploymentConfig(
            STATUS_PROBE_TIMEOUT_SECONDS=0.01,
            STATUS_TOTAL_TIMEOUT_SECONDS=0.05,
        ),
        component_probes=probes,
        cache_enabled=False,
    )
    loop = asyncio.get_running_loop()
    started = loop.time()

    try:
        result = await service.get_status()
    finally:
        release.set()
        await asyncio.sleep(0)

    assert loop.time() - started < 0.15
    assert cancellation_seen.is_set()
    assert _components(result)["telegram_bot"].status is PublicStatusLevel.OUTAGE


@pytest.mark.asyncio
async def test_cancelling_collection_cancels_all_component_probes() -> None:
    started = 0
    cancelled = 0
    all_started = asyncio.Event()

    async def _blocked() -> PublicStatusLevel:
        nonlocal started, cancelled
        started += 1
        if started == len(_COMPONENT_IDS):
            all_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled += 1

    service = PublicStatusService(
        deployment=DeploymentConfig(),
        component_probes=dict.fromkeys(_COMPONENT_IDS, _blocked),
        cache_enabled=False,
    )
    task = asyncio.create_task(service.get_status())
    await asyncio.wait_for(all_started.wait(), timeout=0.5)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == len(_COMPONENT_IDS)


@pytest.mark.asyncio
async def test_health_details_are_never_exposed() -> None:
    probes = _probes()
    probes["postgresql"] = _probe(
        {
            "status": "unhealthy",
            "error": "postgresql://admin:secret@private-db/internal",
            "url": "http://private-host:6333",
            "provider_key": "sk-secret",
        }
    )
    service = PublicStatusService(
        deployment=DeploymentConfig(), component_probes=probes, cache_enabled=False
    )

    serialized = (await service.get_status()).model_dump_json()

    assert "secret" not in serialized
    assert "private" not in serialized
    assert "postgresql://" not in serialized


@pytest.mark.asyncio
async def test_status_response_is_cached() -> None:
    calls = 0

    async def _counted() -> PublicStatusLevel:
        nonlocal calls
        calls += 1
        return PublicStatusLevel.OPERATIONAL

    probes = _probes()
    probes["api"] = _counted
    service = PublicStatusService(deployment=DeploymentConfig(), component_probes=probes)

    first = await service.get_status()
    second = await service.get_status()

    assert calls == 1
    assert second.generated_at == first.generated_at


def _stub_response() -> PublicStatusResponse:
    now = datetime.now(UTC)
    component = PublicStatusComponent(
        id="api",
        name="API",
        status=PublicStatusLevel.OPERATIONAL,
        message="Operational",
        checked_at=now,
        latency_ms=0,
    )
    return PublicStatusResponse(
        status=PublicStatusLevel.OPERATIONAL,
        message="All systems operational",
        generated_at=now,
        refresh_after_seconds=30,
        summary=PublicStatusSummary(
            total=1,
            operational=1,
            degraded=0,
            outage=0,
            unknown=0,
            disabled=0,
        ),
        groups=[
            PublicStatusGroup(
                id="interfaces",
                name="Interfaces",
                status=PublicStatusLevel.OPERATIONAL,
                components=[component],
            )
        ],
    )


def test_status_endpoint_is_public_and_uses_success_envelope() -> None:
    class _StubService:
        async def get_status(self, _request: Any) -> PublicStatusResponse:
            return _stub_response()

    app.dependency_overrides[get_public_status_service] = _StubService
    try:
        response = TestClient(app).get("/v1/status")
    finally:
        app.dependency_overrides.pop(get_public_status_service, None)

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["status"] == "operational"
    assert "security" not in app.openapi()["paths"]["/v1/status"]["get"]
    assert (
        app.openapi()["paths"]["/v1/status"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"]
        == "#/components/schemas/PublicStatusSuccessResponse"
    )
