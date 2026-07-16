"""Bounded, sanitized aggregation for the public status page."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from app.api.models.responses.status import (
    PublicStatusComponent,
    PublicStatusGroup,
    PublicStatusLevel,
    PublicStatusResponse,
    PublicStatusSummary,
)
from app.api.routers.health import (
    _check_database,
    _check_redis,
    _check_scraper,
    _check_vector_store,
)
from app.config import load_config
from app.core.time_utils import UTC
from app.observability.metrics_status import record_status_check

if TYPE_CHECKING:
    from fastapi import Request

    from app.config.deployment import DeploymentConfig

StatusProbe = Callable[[], Awaitable[dict[str, Any] | PublicStatusLevel | str]]

_STATUS_MESSAGES = {
    PublicStatusLevel.OPERATIONAL: "Operational",
    PublicStatusLevel.DEGRADED: "Operating with reduced capability",
    PublicStatusLevel.OUTAGE: "Currently unavailable",
    PublicStatusLevel.UNKNOWN: "Status unavailable",
    PublicStatusLevel.DISABLED: "Disabled",
}
_OVERALL_MESSAGES = {
    PublicStatusLevel.OPERATIONAL: "All systems operational",
    PublicStatusLevel.DEGRADED: "Some systems have limited or unavailable status",
    PublicStatusLevel.OUTAGE: "A critical system is unavailable",
    PublicStatusLevel.UNKNOWN: "System status unavailable",
    PublicStatusLevel.DISABLED: "All systems disabled",
}
_HEALTH_LEVELS = {
    "healthy": PublicStatusLevel.OPERATIONAL,
    "operational": PublicStatusLevel.OPERATIONAL,
    "degraded": PublicStatusLevel.DEGRADED,
    "disabled": PublicStatusLevel.DISABLED,
    "unhealthy": PublicStatusLevel.OUTAGE,
    "unavailable": PublicStatusLevel.OUTAGE,
    "error": PublicStatusLevel.OUTAGE,
    "timeout": PublicStatusLevel.OUTAGE,
    "outage": PublicStatusLevel.OUTAGE,
    "unknown": PublicStatusLevel.UNKNOWN,
}
_MAX_METRICS_RESPONSE_BYTES = 256 * 1024
_OPENROUTER_CIRCUIT_METRIC = "openrouter_circuit_breaker_state"


@dataclass(frozen=True, slots=True)
class _ComponentSpec:
    id: str
    name: str
    group_id: str
    critical: bool = False


_GROUPS = (
    ("interfaces", "Interfaces"),
    ("data", "Data services"),
    ("processing", "Processing"),
)
_COMPONENTS = (
    _ComponentSpec("api", "API", "interfaces", critical=True),
    _ComponentSpec("web_application", "Web application", "interfaces", critical=True),
    _ComponentSpec("telegram_bot", "Telegram bot", "interfaces", critical=True),
    _ComponentSpec("postgresql", "PostgreSQL", "data", critical=True),
    _ComponentSpec("redis", "Redis", "data"),
    _ComponentSpec("vector_search", "Qdrant / vector search", "data"),
    _ComponentSpec("extraction", "Scraper / extraction", "processing", critical=True),
    _ComponentSpec("ai_summarization", "AI summarization", "processing", critical=True),
    _ComponentSpec("taskiq_worker", "Taskiq worker", "processing", critical=True),
    _ComponentSpec("scheduler", "Scheduler", "processing", critical=True),
)


class _StatusCache:
    def __init__(self) -> None:
        self.value: PublicStatusResponse | None = None
        self.cached_at = 0.0
        self.lock = asyncio.Lock()

    def clear(self) -> None:
        self.value = None
        self.cached_at = 0.0


_status_cache = _StatusCache()


def clear_status_cache() -> None:
    """Clear the process-local public status cache (primarily for tests)."""
    _status_cache.clear()


class PublicStatusService:
    """Collect public-safe status signals within a strict time budget."""

    def __init__(
        self,
        *,
        deployment: DeploymentConfig,
        component_probes: Mapping[str, StatusProbe] | None = None,
        web_index_path: Path | None = None,
        llm_provider: str = "openrouter",
        cache_enabled: bool = True,
    ) -> None:
        self._deployment = deployment
        self._component_probes = dict(component_probes or {})
        self._web_index_path = web_index_path or (
            Path(__file__).resolve().parents[2] / "static" / "web" / "index.html"
        )
        self._llm_provider = llm_provider.strip().lower()
        self._cache_enabled = cache_enabled

    async def get_status(self, request: Request | None = None) -> PublicStatusResponse:
        """Return a cached status payload or collect all checks concurrently."""
        now = time.monotonic()
        cached = _status_cache.value
        if (
            self._cache_enabled
            and cached is not None
            and now - _status_cache.cached_at < self._deployment.status_cache_ttl_seconds
        ):
            return cached.model_copy(deep=True)

        async with _status_cache.lock:
            now = time.monotonic()
            cached = _status_cache.value
            if (
                self._cache_enabled
                and cached is not None
                and now - _status_cache.cached_at < self._deployment.status_cache_ttl_seconds
            ):
                return cached.model_copy(deep=True)

            result = await self._collect(request)
            if self._cache_enabled:
                _status_cache.value = result.model_copy(deep=True)
                _status_cache.cached_at = time.monotonic()
            return result

    async def _collect(self, request: Request | None) -> PublicStatusResponse:
        probes = self._build_probes(request)
        tasks = {
            spec.id: asyncio.create_task(self._check_component(spec, probes[spec.id]))
            for spec in _COMPONENTS
        }
        try:
            done, pending = await asyncio.wait(
                tasks.values(), timeout=self._deployment.status_total_timeout_seconds
            )
        except BaseException:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            raise
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        done_set = set(done)
        checked_at = datetime.now(UTC)
        timeout_latency_ms = round(self._deployment.status_total_timeout_seconds * 1000, 2)
        components: dict[str, PublicStatusComponent] = {}
        for spec in _COMPONENTS:
            task = tasks[spec.id]
            if task in done_set:
                try:
                    components[spec.id] = task.result()
                    continue
                except BaseException:
                    pass
            components[spec.id] = self._component(
                spec,
                PublicStatusLevel.OUTAGE,
                checked_at=checked_at,
                latency_ms=timeout_latency_ms,
            )
            record_status_check(
                spec.id,
                PublicStatusLevel.OUTAGE.value,
                self._deployment.status_total_timeout_seconds,
            )

        groups = [
            PublicStatusGroup(
                id=group_id,
                name=group_name,
                status=self._aggregate_levels(
                    [
                        components[spec.id].status
                        for spec in _COMPONENTS
                        if spec.group_id == group_id
                    ]
                ),
                components=[
                    components[spec.id] for spec in _COMPONENTS if spec.group_id == group_id
                ],
            )
            for group_id, group_name in _GROUPS
        ]
        levels = [component.status for component in components.values()]
        overall = self._overall_status(components)
        counts = {level: levels.count(level) for level in PublicStatusLevel}
        return PublicStatusResponse(
            status=overall,
            message=_OVERALL_MESSAGES[overall],
            generated_at=datetime.now(UTC),
            refresh_after_seconds=self._deployment.status_refresh_after_seconds,
            summary=PublicStatusSummary(
                total=len(levels),
                operational=counts[PublicStatusLevel.OPERATIONAL],
                degraded=counts[PublicStatusLevel.DEGRADED],
                outage=counts[PublicStatusLevel.OUTAGE],
                unknown=counts[PublicStatusLevel.UNKNOWN],
                disabled=counts[PublicStatusLevel.DISABLED],
            ),
            groups=groups,
        )

    def _build_probes(self, request: Request | None) -> dict[str, StatusProbe]:
        worker_metrics_task: asyncio.Task[
            tuple[PublicStatusLevel, bytes | None]
        ] | None = None

        async def _worker_metrics() -> tuple[PublicStatusLevel, bytes | None]:
            nonlocal worker_metrics_task
            if worker_metrics_task is None:
                worker_metrics_task = asyncio.create_task(
                    self._fetch_metrics(self._deployment.status_worker_metrics_url)
                )
            return await worker_metrics_task

        async def _api() -> PublicStatusLevel:
            return PublicStatusLevel.OPERATIONAL

        async def _web_application() -> PublicStatusLevel:
            try:
                available = self._web_index_path.is_file() and self._web_index_path.stat().st_size > 0
            except OSError:
                available = False
            return (
                PublicStatusLevel.OPERATIONAL if available else PublicStatusLevel.UNKNOWN
            )

        async def _ai_summarization() -> PublicStatusLevel:
            if self._llm_provider != "openrouter":
                return PublicStatusLevel.UNKNOWN
            process_level, payload = await _worker_metrics()
            if process_level is not PublicStatusLevel.OPERATIONAL or payload is None:
                return PublicStatusLevel.UNKNOWN
            return self._parse_openrouter_status(payload)

        async def _worker() -> PublicStatusLevel:
            process_level, _payload = await _worker_metrics()
            return process_level

        async def _database() -> dict[str, Any]:
            return await _check_database(include_details=False, request=request)

        async def _vector() -> dict[str, Any]:
            return await _check_vector_store(request)

        probes: dict[str, StatusProbe] = {
            "api": _api,
            "web_application": _web_application,
            "telegram_bot": lambda: self._probe_process(
                self._deployment.status_bot_metrics_url
            ),
            "postgresql": _database,
            "redis": _check_redis,
            "vector_search": _vector,
            "extraction": _check_scraper,
            "ai_summarization": _ai_summarization,
            "taskiq_worker": _worker,
            "scheduler": lambda: self._probe_process(
                self._deployment.status_scheduler_metrics_url
            ),
        }
        probes.update(self._component_probes)
        return probes

    async def _probe_process(self, url: str | None) -> PublicStatusLevel:
        level, _payload = await self._fetch_metrics(url)
        return level

    async def _fetch_metrics(
        self, url: str | None
    ) -> tuple[PublicStatusLevel, bytes | None]:
        if url is None:
            return PublicStatusLevel.UNKNOWN, None
        try:
            timeout = httpx.Timeout(self._deployment.status_probe_timeout_seconds)
            async with (
                httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client,
                client.stream("GET", url, headers={"Accept": "text/plain"}) as response,
            ):
                content_type = response.headers.get("content-type", "").lower()
                if not response.is_success or "text/plain" not in content_type:
                    return PublicStatusLevel.OUTAGE, None
                payload = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(payload) + len(chunk) > _MAX_METRICS_RESPONSE_BYTES:
                        return PublicStatusLevel.OPERATIONAL, None
                    payload.extend(chunk)
                return PublicStatusLevel.OPERATIONAL, bytes(payload)
        except (httpx.HTTPError, TimeoutError):
            pass
        return PublicStatusLevel.OUTAGE, None

    @staticmethod
    def _parse_openrouter_status(payload: bytes) -> PublicStatusLevel:
        values: list[float] = []
        for raw_line in payload.decode("utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line.startswith(
                (f"{_OPENROUTER_CIRCUIT_METRIC}{{", f"{_OPENROUTER_CIRCUIT_METRIC} ")
            ):
                continue
            if "}" in line:
                value_text = line.rsplit("}", 1)[-1].strip()
            else:
                value_text = line.removeprefix(_OPENROUTER_CIRCUIT_METRIC).strip()
            fields = value_text.split()
            if not fields:
                continue
            try:
                value = float(fields[0])
            except ValueError:
                continue
            if math.isfinite(value):
                values.append(value)
        if not values:
            return PublicStatusLevel.UNKNOWN
        if all(value >= 2 for value in values):
            return PublicStatusLevel.OUTAGE
        if any(value >= 1 for value in values):
            return PublicStatusLevel.DEGRADED
        return PublicStatusLevel.OPERATIONAL

    async def _check_component(
        self, spec: _ComponentSpec, probe: StatusProbe
    ) -> PublicStatusComponent:
        start = time.perf_counter()
        try:
            raw = await asyncio.wait_for(
                probe(), timeout=self._deployment.status_probe_timeout_seconds
            )
            level = self._map_level(raw)
        except Exception:
            level = PublicStatusLevel.OUTAGE
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        record_status_check(spec.id, level.value, latency_ms / 1000)
        return self._component(
            spec,
            level,
            checked_at=datetime.now(UTC),
            latency_ms=latency_ms,
        )

    @staticmethod
    def _map_level(raw: dict[str, Any] | PublicStatusLevel | str) -> PublicStatusLevel:
        if isinstance(raw, dict):
            raw = str(raw.get("status", "unknown"))
        if isinstance(raw, PublicStatusLevel):
            return raw
        return _HEALTH_LEVELS.get(str(raw).strip().lower(), PublicStatusLevel.UNKNOWN)

    @staticmethod
    def _component(
        spec: _ComponentSpec,
        level: PublicStatusLevel,
        *,
        checked_at: datetime,
        latency_ms: float,
    ) -> PublicStatusComponent:
        return PublicStatusComponent(
            id=spec.id,
            name=spec.name,
            status=level,
            message=_STATUS_MESSAGES[level],
            checked_at=checked_at,
            latency_ms=latency_ms,
        )

    @staticmethod
    def _aggregate_levels(levels: list[PublicStatusLevel]) -> PublicStatusLevel:
        active = [level for level in levels if level is not PublicStatusLevel.DISABLED]
        if not active:
            return PublicStatusLevel.DISABLED
        for level in (
            PublicStatusLevel.OUTAGE,
            PublicStatusLevel.DEGRADED,
            PublicStatusLevel.UNKNOWN,
        ):
            if level in active:
                return level
        return PublicStatusLevel.OPERATIONAL

    @staticmethod
    def _overall_status(
        components: Mapping[str, PublicStatusComponent],
    ) -> PublicStatusLevel:
        active = [
            component
            for component in components.values()
            if component.status is not PublicStatusLevel.DISABLED
        ]
        if not active:
            return PublicStatusLevel.DISABLED
        critical_ids = {spec.id for spec in _COMPONENTS if spec.critical}
        if any(
            component.id in critical_ids and component.status is PublicStatusLevel.OUTAGE
            for component in active
        ):
            return PublicStatusLevel.OUTAGE
        if any(
            component.status
            in {PublicStatusLevel.OUTAGE, PublicStatusLevel.DEGRADED, PublicStatusLevel.UNKNOWN}
            for component in active
        ):
            return PublicStatusLevel.DEGRADED
        return PublicStatusLevel.OPERATIONAL


def get_public_status_service() -> PublicStatusService:
    """Build the public status service from validated application configuration."""
    config = load_config(allow_stub_telegram=True)
    return PublicStatusService(
        deployment=config.deployment,
        llm_provider=config.runtime.llm_provider,
    )
