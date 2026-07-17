"""Bounded, sanitized aggregation for the public status page."""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select

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
from app.db.models.ai_backup import AiAccountBackup, AiBackupService, AiBackupStatus
from app.db.models.git_backup import GitMirror, GitMirrorSource, GitMirrorStatus
from app.observability.metrics_status import record_status_check

if TYPE_CHECKING:
    from fastapi import Request

    from app.config.deployment import DeploymentConfig
    from app.db.session import Database

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
_PG_BACKUP_LAST_SUCCESS_METRIC = "ratatoskr_pg_backup_last_success_timestamp_seconds"
_VECTOR_RECONCILE_RUNS_METRIC = "ratatoskr_vector_reconcile_runs_total"
_VECTOR_RECONCILE_LAG_METRIC = "ratatoskr_vector_reconcile_oldest_lag_seconds"
_BACKUP_STALE_AFTER = timedelta(hours=36)
_BACKUP_OUTAGE_AFTER = timedelta(hours=48)
_VECTOR_RECONCILE_LAG_WARNING_SECONDS = 3600


@dataclass(frozen=True, slots=True)
class _StatusSignal:
    level: PublicStatusLevel
    message: str


StatusProbe = Callable[[], Awaitable[dict[str, Any] | PublicStatusLevel | str | _StatusSignal]]


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
    ("backups", "Backups"),
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
    _ComponentSpec("vector_reconciliation", "Vector reconciliation", "processing"),
    _ComponentSpec("postgresql_backup", "PostgreSQL backup", "backups"),
    _ComponentSpec("github_repository_backups", "GitHub repository backups", "backups"),
    _ComponentSpec("chatgpt_backup", "ChatGPT backup authorization", "backups"),
    _ComponentSpec("claude_backup", "Claude backup authorization", "backups"),
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
        database: Database | None = None,
        git_backup_enabled: bool = False,
        ai_backup_enabled: bool = False,
        chatgpt_backup_enabled: bool = False,
        claude_backup_enabled: bool = False,
        cache_enabled: bool = True,
    ) -> None:
        self._deployment = deployment
        self._component_probes = dict(component_probes or {})
        self._web_index_path = web_index_path or (
            Path(__file__).resolve().parents[2] / "static" / "web" / "index.html"
        )
        self._llm_provider = llm_provider.strip().lower()
        self._database = database
        self._git_backup_enabled = git_backup_enabled
        self._ai_backup_enabled = ai_backup_enabled
        self._chatgpt_backup_enabled = chatgpt_backup_enabled
        self._claude_backup_enabled = claude_backup_enabled
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
        except asyncio.CancelledError:
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
            if task in done_set and not task.cancelled():
                try:
                    components[spec.id] = task.result()
                    continue
                except Exception:
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
        worker_metrics_task: asyncio.Task[tuple[PublicStatusLevel, bytes | None]] | None = None
        node_metrics_task: asyncio.Task[tuple[PublicStatusLevel, bytes | None]] | None = None

        async def _worker_metrics() -> tuple[PublicStatusLevel, bytes | None]:
            nonlocal worker_metrics_task
            if worker_metrics_task is None:
                worker_metrics_task = asyncio.create_task(
                    self._fetch_metrics(self._deployment.status_worker_metrics_url)
                )
            return await worker_metrics_task

        async def _node_metrics() -> tuple[PublicStatusLevel, bytes | None]:
            nonlocal node_metrics_task
            if node_metrics_task is None:
                node_metrics_task = asyncio.create_task(
                    self._fetch_metrics(self._deployment.status_node_metrics_url)
                )
            return await node_metrics_task

        async def _api() -> PublicStatusLevel:
            return PublicStatusLevel.OPERATIONAL

        async def _web_application() -> PublicStatusLevel:
            try:
                available = (
                    self._web_index_path.is_file() and self._web_index_path.stat().st_size > 0
                )
            except OSError:
                available = False
            return PublicStatusLevel.OPERATIONAL if available else PublicStatusLevel.UNKNOWN

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

        async def _vector_reconciliation() -> _StatusSignal:
            process_level, payload = await _worker_metrics()
            if process_level is not PublicStatusLevel.OPERATIONAL or payload is None:
                return _StatusSignal(PublicStatusLevel.UNKNOWN, "Reconciliation status unavailable")
            return self._parse_vector_reconciliation_status(payload)

        async def _postgresql_backup() -> _StatusSignal:
            process_level, payload = await _node_metrics()
            if process_level is not PublicStatusLevel.OPERATIONAL or payload is None:
                return _StatusSignal(PublicStatusLevel.UNKNOWN, "Backup status unavailable")
            return self._parse_postgresql_backup_status(payload)

        async def _github_repository_backups() -> _StatusSignal:
            if not self._git_backup_enabled:
                return _StatusSignal(PublicStatusLevel.DISABLED, "Disabled")
            if self._database is None:
                return _StatusSignal(PublicStatusLevel.UNKNOWN, "Backup status unavailable")
            async with self._database.session() as session:
                rows = (
                    await session.execute(
                        select(GitMirror.status, GitMirror.last_mirrored_at).where(
                            GitMirror.source == GitMirrorSource.GITHUB
                        )
                    )
                ).all()
            return self._github_backup_status(list(rows))

        async def _ai_backup(service: AiBackupService, *, enabled: bool) -> _StatusSignal:
            if not self._ai_backup_enabled or not enabled:
                return _StatusSignal(PublicStatusLevel.DISABLED, "Disabled")
            if self._database is None:
                return _StatusSignal(PublicStatusLevel.UNKNOWN, "Backup status unavailable")
            async with self._database.session() as session:
                rows = (
                    await session.execute(
                        select(AiAccountBackup.status, AiAccountBackup.last_backed_up_at).where(
                            AiAccountBackup.service == service
                        )
                    )
                ).all()
            return self._ai_backup_status(list(rows))

        async def _database() -> dict[str, Any]:
            return await _check_database(include_details=False, request=request)

        async def _vector() -> dict[str, Any]:
            return await _check_vector_store(request)

        probes: dict[str, StatusProbe] = {
            "api": _api,
            "web_application": _web_application,
            "telegram_bot": lambda: self._probe_process(self._deployment.status_bot_metrics_url),
            "postgresql": _database,
            "redis": _check_redis,
            "vector_search": _vector,
            "extraction": _check_scraper,
            "ai_summarization": _ai_summarization,
            "taskiq_worker": _worker,
            "scheduler": lambda: self._probe_process(self._deployment.status_scheduler_metrics_url),
            "vector_reconciliation": _vector_reconciliation,
            "postgresql_backup": _postgresql_backup,
            "github_repository_backups": _github_repository_backups,
            "chatgpt_backup": lambda: _ai_backup(
                AiBackupService.CHATGPT, enabled=self._chatgpt_backup_enabled
            ),
            "claude_backup": lambda: _ai_backup(
                AiBackupService.CLAUDE, enabled=self._claude_backup_enabled
            ),
        }
        probes.update(self._component_probes)
        return probes

    async def _probe_process(self, url: str | None) -> PublicStatusLevel:
        level, _payload = await self._fetch_metrics(url)
        return level

    async def _fetch_metrics(self, url: str | None) -> tuple[PublicStatusLevel, bytes | None]:
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

    @staticmethod
    def _metric_values(payload: bytes, metric: str) -> list[tuple[str, float]]:
        values: list[tuple[str, float]] = []
        for raw_line in payload.decode("utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line.startswith((f"{metric}{{", f"{metric} ")):
                continue
            sample, _, value_text = line.rpartition(" ")
            try:
                value = float(value_text)
            except ValueError:
                continue
            if math.isfinite(value):
                values.append((sample, value))
        return values

    @classmethod
    def _parse_postgresql_backup_status(
        cls, payload: bytes, *, now: datetime | None = None
    ) -> _StatusSignal:
        values = cls._metric_values(payload, _PG_BACKUP_LAST_SUCCESS_METRIC)
        if not values:
            return _StatusSignal(PublicStatusLevel.OUTAGE, "No successful backup observed")
        now = now or datetime.now(UTC)
        age = now - datetime.fromtimestamp(max(value for _sample, value in values), tz=UTC)
        if age < timedelta(minutes=-5):
            return _StatusSignal(PublicStatusLevel.UNKNOWN, "Backup timestamp is invalid")
        if age > _BACKUP_OUTAGE_AFTER:
            return _StatusSignal(PublicStatusLevel.OUTAGE, "Latest backup is overdue")
        if age > _BACKUP_STALE_AFTER:
            return _StatusSignal(PublicStatusLevel.DEGRADED, "Latest backup is stale")
        return _StatusSignal(PublicStatusLevel.OPERATIONAL, "Latest backup is current")

    @classmethod
    def _parse_vector_reconciliation_status(cls, payload: bytes) -> _StatusSignal:
        runs = cls._metric_values(payload, _VECTOR_RECONCILE_RUNS_METRIC)
        if not runs:
            return _StatusSignal(PublicStatusLevel.UNKNOWN, "No reconciliation run observed")
        successes = sum(value for sample, value in runs if 'status="success"' in sample)
        failures = sum(value for sample, value in runs if 'status="error"' in sample)
        if successes <= 0 and failures > 0:
            return _StatusSignal(PublicStatusLevel.OUTAGE, "Reconciliation runs are failing")
        lag = cls._metric_values(payload, _VECTOR_RECONCILE_LAG_METRIC)
        if lag and max(value for _sample, value in lag) > _VECTOR_RECONCILE_LAG_WARNING_SECONDS:
            return _StatusSignal(PublicStatusLevel.DEGRADED, "Reconciliation is behind")
        return _StatusSignal(PublicStatusLevel.OPERATIONAL, "Reconciliation is current")

    @staticmethod
    def _freshness_level(last_success: datetime | None, *, now: datetime) -> PublicStatusLevel:
        if last_success is None:
            return PublicStatusLevel.UNKNOWN
        if last_success.tzinfo is None:
            last_success = last_success.replace(tzinfo=UTC)
        age = now - last_success
        if age > _BACKUP_OUTAGE_AFTER:
            return PublicStatusLevel.OUTAGE
        if age > _BACKUP_STALE_AFTER:
            return PublicStatusLevel.DEGRADED
        return PublicStatusLevel.OPERATIONAL

    @classmethod
    def _github_backup_status(
        cls, rows: list[Any], *, now: datetime | None = None
    ) -> _StatusSignal:
        active = [row for row in rows if row.status != GitMirrorStatus.EXCLUDED]
        if not active:
            return _StatusSignal(PublicStatusLevel.UNKNOWN, "No repository backup observed")
        now = now or datetime.now(UTC)
        levels: list[PublicStatusLevel] = []
        for row in active:
            if row.status == GitMirrorStatus.OK:
                levels.append(cls._freshness_level(row.last_mirrored_at, now=now))
            elif row.status == GitMirrorStatus.PENDING:
                levels.append(PublicStatusLevel.UNKNOWN)
            else:
                levels.append(PublicStatusLevel.OUTAGE)
        if all(level is PublicStatusLevel.OPERATIONAL for level in levels):
            return _StatusSignal(PublicStatusLevel.OPERATIONAL, "Repository backups are current")
        if all(level in {PublicStatusLevel.OUTAGE, PublicStatusLevel.UNKNOWN} for level in levels):
            level = (
                PublicStatusLevel.OUTAGE
                if PublicStatusLevel.OUTAGE in levels
                else PublicStatusLevel.UNKNOWN
            )
            return _StatusSignal(level, "Repository backups need attention")
        return _StatusSignal(PublicStatusLevel.DEGRADED, "Repository backup coverage is partial")

    @classmethod
    def _ai_backup_status(cls, rows: list[Any], *, now: datetime | None = None) -> _StatusSignal:
        if not rows:
            return _StatusSignal(PublicStatusLevel.UNKNOWN, "Authorization has not been verified")
        if any(row.status == AiBackupStatus.AUTH_EXPIRED for row in rows):
            return _StatusSignal(PublicStatusLevel.OUTAGE, "Authorization required")
        now = now or datetime.now(UTC)
        levels: list[PublicStatusLevel] = []
        for row in rows:
            if row.status == AiBackupStatus.OK:
                levels.append(cls._freshness_level(row.last_backed_up_at, now=now))
            elif row.status == AiBackupStatus.DISABLED:
                levels.append(PublicStatusLevel.DISABLED)
            elif row.status == AiBackupStatus.PENDING:
                levels.append(PublicStatusLevel.UNKNOWN)
            else:
                levels.append(PublicStatusLevel.OUTAGE)
        level = cls._aggregate_levels(levels)
        if level is PublicStatusLevel.OPERATIONAL:
            message = "Authorization active; backup is current"
        elif level is PublicStatusLevel.DISABLED:
            message = "Disabled"
        elif level is PublicStatusLevel.UNKNOWN:
            message = "Authorization has not been verified"
        elif level is PublicStatusLevel.DEGRADED:
            message = "Backup is stale"
        else:
            message = "Backup is unavailable"
        return _StatusSignal(level, message)

    async def _check_component(
        self, spec: _ComponentSpec, probe: StatusProbe
    ) -> PublicStatusComponent:
        start = time.perf_counter()
        try:
            raw = await asyncio.wait_for(
                probe(), timeout=self._deployment.status_probe_timeout_seconds
            )
            if isinstance(raw, _StatusSignal):
                signal = raw
                level = raw.level
            else:
                signal = None
                level = self._map_level(raw)
        except Exception:
            level = PublicStatusLevel.OUTAGE
            signal = None
        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        record_status_check(spec.id, level.value, latency_ms / 1000)
        return self._component(
            spec,
            level,
            checked_at=datetime.now(UTC),
            latency_ms=latency_ms,
            message=signal.message if signal is not None else None,
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
        message: str | None = None,
    ) -> PublicStatusComponent:
        return PublicStatusComponent(
            id=spec.id,
            name=spec.name,
            status=level,
            message=message or _STATUS_MESSAGES[level],
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
    try:
        from app.di.api import get_current_api_runtime

        runtime = get_current_api_runtime()
    except RuntimeError:
        runtime = None
    config = runtime.cfg if runtime is not None else load_config(allow_stub_telegram=True)
    return PublicStatusService(
        deployment=config.deployment,
        llm_provider=config.runtime.llm_provider,
        database=runtime.db if runtime is not None else None,
        git_backup_enabled=config.git_backup.enabled,
        ai_backup_enabled=config.ai_backup.enabled,
        chatgpt_backup_enabled=config.ai_backup.chatgpt_enabled,
        claude_backup_enabled=config.ai_backup.claude_enabled,
    )
