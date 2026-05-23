"""Diagnostics composition service for owner-only operational dashboards."""

from __future__ import annotations

import asyncio
import datetime as _dt
import re
import time
from typing import TYPE_CHECKING, Any, cast

from app.api.dependencies.database import get_session_manager
from app.api.models.responses.diagnostics import (
    DiagnosticsComponent,
    DiagnosticsProviderStatus,
    DiagnosticsQueueBacklog,
    DiagnosticsResponse,
    DiagnosticsStorageGrowth,
    DiagnosticsSyncFailure,
    DiagnosticsVectorIndexLag,
    HealthStatus,
)
from app.api.services.system_maintenance_service import SystemMaintenanceService
from app.core.time_utils import UTC
from app.db.session import Database  # noqa: TC001  # used at runtime in __init__ signature
from app.infrastructure.embedding.embedding_service import DEFAULT_MODELS
from app.infrastructure.persistence.repositories.admin_read_repository import (
    AdminReadRepositoryAdapter,
)
from app.infrastructure.vector.reconciliation import VectorIndexReconciler

if TYPE_CHECKING:
    from fastapi import Request


DIAGNOSTICS_CACHE_TTL_SECONDS = 30


class _DiagnosticsCache:
    def __init__(self) -> None:
        self.value: DiagnosticsResponse | None = None
        self.cached_at: float = 0.0
        self.lock = asyncio.Lock()

    def fresh(self, now: float) -> bool:
        return self.value is not None and now - self.cached_at < DIAGNOSTICS_CACHE_TTL_SECONDS


_diagnostics_cache = _DiagnosticsCache()


class DiagnosticsService:
    """Own provider health, vector lag, storage growth, and sync failure diagnostics."""

    def __init__(
        self,
        session_manager: Database | None = None,
        *,
        vector_store: Any | None = None,
        admin_repo: Any | None = None,
    ) -> None:
        self._db = session_manager or get_session_manager()
        self._admin_repo = admin_repo or AdminReadRepositoryAdapter(self._db)
        self._vector_store = vector_store

    async def diagnostics(self, *, request: Request | None = None) -> DiagnosticsResponse:
        """Return owner-safe operational diagnostics with a short process-local cache."""
        now_monotonic = time.monotonic()
        if _diagnostics_cache.fresh(now_monotonic):
            assert _diagnostics_cache.value is not None
            return _diagnostics_cache.value

        async with _diagnostics_cache.lock:
            now_monotonic = time.monotonic()
            if _diagnostics_cache.fresh(now_monotonic):
                assert _diagnostics_cache.value is not None
                return _diagnostics_cache.value
            response = await self._build_diagnostics(request=request)
            _diagnostics_cache.value = response
            _diagnostics_cache.cached_at = now_monotonic
            return response

    async def _build_diagnostics(self, *, request: Request | None) -> DiagnosticsResponse:
        from app.adapters.content.scraper.diagnostics import build_scraper_diagnostics
        from app.api.routers import health
        from app.config import load_config

        now = _dt.datetime.now(UTC)
        since = now - _dt.timedelta(days=7)
        persisted = await self._admin_repo.async_diagnostics_snapshot(since=since, now=now)
        db_info = await SystemMaintenanceService(database=self._db).get_db_info()

        health_results = await asyncio.gather(
            health._check_database(include_details=False, request=request),
            health._check_redis(),
            health._check_vector_store(request),
            return_exceptions=True,
        )
        db_status, redis_status, qdrant_status = health_results
        scraper_config = build_scraper_diagnostics(load_config(allow_stub_telegram=True))
        cfg = load_config(allow_stub_telegram=True)
        vector_report = await VectorIndexReconciler(
            database=self._db,
            vector_store=self._vector_store,
            expected_summary_models=_expected_embedding_models(cfg),
            expected_repository_models=_expected_embedding_models(cfg),
            expected_model_version="1.0",
            scan_limit=cfg.vector_reconcile.batch_size,
        ).inspect(now=now)
        scraper_providers = _merge_scraper_provider_status(
            scraper_config=scraper_config,
            persisted=list(persisted["scraper_providers"]),
        )
        llm_providers = [
            DiagnosticsProviderStatus.model_validate(item) for item in persisted["llm_providers"]
        ]
        llm_failure_count = sum(provider.failure_count for provider in llm_providers)
        integration_health = persisted["integration_health"]
        components = {
            "postgresql": _component_from_health(db_status, checked_at=now),
            "redis": _component_from_health(redis_status, checked_at=now),
            "qdrant": _component_from_health(qdrant_status, checked_at=now),
            "scraper": DiagnosticsComponent(
                status=_status(scraper_config.get("status")),
                failure_count=sum(provider.failure_count for provider in scraper_providers),
                checked_at=now,
                details={
                    "enabled": bool(scraper_config.get("enabled")),
                    "provider_order_effective": list(
                        scraper_config.get("provider_order_effective") or []
                    ),
                    "configured_provider_count": len(scraper_config.get("providers") or {}),
                },
            ),
            "llm": DiagnosticsComponent(
                status="healthy" if llm_failure_count == 0 else "degraded",
                failure_count=llm_failure_count,
                checked_at=now,
                details={"provider_count": len(llm_providers)},
            ),
            "rss": DiagnosticsComponent.model_validate(integration_health["rss"]),
            "github": DiagnosticsComponent.model_validate(integration_health["github"]),
        }
        storage_activity = persisted["storage_activity"]
        table_counts = db_info.get("table_counts")
        return DiagnosticsResponse(
            generated_at=now,
            cache_ttl_seconds=DIAGNOSTICS_CACHE_TTL_SECONDS,
            components=components,
            scraper_providers=scraper_providers,
            llm_providers=llm_providers,
            queue_backlog=DiagnosticsQueueBacklog.model_validate(persisted["queue_backlog"]),
            vector_indexing_lag=DiagnosticsVectorIndexLag.model_validate(
                vector_report.to_diagnostics()
            ),
            latest_sync_failures=[
                DiagnosticsSyncFailure.model_validate(item)
                for item in persisted["latest_sync_failures"]
            ],
            storage_growth=DiagnosticsStorageGrowth(
                database_size_mb=_safe_float(db_info.get("database_size_mb")),
                table_counts={
                    str(name): int(count)
                    for name, count in (
                        table_counts.items() if isinstance(table_counts, dict) else ()
                    )
                    if isinstance(count, int) and count >= 0
                },
                created_last_24h=storage_activity["created_last_24h"],
                created_last_7d=storage_activity["created_last_7d"],
            ),
        )


def clear_diagnostics_cache() -> None:
    _diagnostics_cache.value = None
    _diagnostics_cache.cached_at = 0.0


def _status(value: object) -> HealthStatus:
    text = str(value or "unknown")
    if text in {"healthy", "degraded", "unhealthy", "disabled", "unavailable"}:
        return cast("HealthStatus", text)
    if text == "timeout":
        return "unhealthy"
    return "unknown"


def _component_from_health(value: object, *, checked_at: _dt.datetime) -> DiagnosticsComponent:
    if isinstance(value, BaseException):
        return DiagnosticsComponent(
            status="unhealthy",
            failure_count=1,
            last_error_code=value.__class__.__name__,
            last_error_message=_redact_message(str(value)),
            checked_at=checked_at,
        )
    if not isinstance(value, dict):
        return DiagnosticsComponent(status="unknown", checked_at=checked_at)

    error = value.get("error")
    status = _status(value.get("status"))
    details = {
        str(key): val
        for key, val in value.items()
        if key
        in {
            "latency_ms",
            "collection",
            "size_mb",
            "size_bytes",
            "integrity_ok",
            "last_attempt",
        }
    }
    return DiagnosticsComponent(
        status=status,
        failure_count=0 if status in {"healthy", "disabled"} else 1,
        last_error_code=status.upper() if error else None,
        last_error_message=_redact_message(str(error)) if error else None,
        checked_at=checked_at,
        details=details,
    )


def _expected_embedding_models(cfg: Any) -> set[str]:
    if getattr(cfg.embedding, "provider", "local") == "gemini":
        return {str(cfg.embedding.gemini_model)}
    return set(DEFAULT_MODELS.values())


def _merge_scraper_provider_status(
    *,
    scraper_config: dict[str, Any],
    persisted: list[dict[str, Any]],
) -> list[DiagnosticsProviderStatus]:
    by_provider = {str(item["provider"]): dict(item) for item in persisted}
    providers = scraper_config.get("providers") if isinstance(scraper_config, dict) else {}
    if isinstance(providers, dict):
        for name, details in providers.items():
            provider = str(name)
            row = by_provider.setdefault(
                provider,
                {
                    "provider": provider,
                    "total_count": 0,
                    "failure_count": 0,
                    "last_error_code": None,
                    "last_error_message": None,
                    "last_failure_at": None,
                },
            )
            if isinstance(details, dict):
                enabled = bool(details.get("enabled"))
                dependency_ready = bool(details.get("dependency_ready", True))
                if not enabled:
                    row["status"] = "disabled"
                elif not dependency_ready:
                    row["status"] = "degraded"
                    row.setdefault("last_error_code", "DEPENDENCY_UNAVAILABLE")
                elif int(row.get("failure_count") or 0) == 0:
                    row["status"] = "healthy"
                else:
                    row["status"] = "degraded"
    return [
        DiagnosticsProviderStatus.model_validate(row)
        for row in sorted(by_provider.values(), key=lambda item: str(item["provider"]))
    ]


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str | int | float):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization)\s*=\s*(bearer|basic)\s+[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)=([^&\s]+)"),
    re.compile(r"(?i)\b(bearer|basic)\s+[a-z0-9._~+/=-]+"),
    re.compile(r"(?i)(sk-[a-z0-9_-]{12,})"),
)


def _redact_message(message: str, *, max_len: int = 240) -> str | None:
    text = message
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_redact_match, text)
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if len(text) > max_len:
        return f"{text[: max_len - 3]}..."
    return text or None


def _redact_match(match: re.Match[str]) -> str:
    if match.lastindex == 1:
        return "[REDACTED]"
    return f"{match.group(1)}=[REDACTED]"
