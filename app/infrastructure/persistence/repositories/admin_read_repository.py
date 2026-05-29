"""SQLAlchemy read adapter for admin dashboards and audit log views.

This module is the public facade over the bounded-context sub-repositories
in ``app.infrastructure.persistence.repositories.admin``.  All callers
continue to import ``AdminReadRepositoryAdapter`` (and the handful of
module-level helpers used by tests) from this path.

Sub-repository layout:
  admin/_helpers.py               -- pure helpers (redaction, parsing, coercion)
  admin/_users.py                 -- user listing with aggregate counts
  admin/_jobs.py                  -- pipeline job status and content health
  admin/_llm.py                   -- LLM metrics and per-provider cost breakdown
  admin/_audit.py                 -- audit log browsing
  admin/_diagnostics.py           -- diagnostics snapshot coordinator
  admin/_diagnostics_providers.py -- LLM/scraper stats, vector lag, queue, storage
  admin/_diagnostics_sync.py      -- social connections and sync failure listing
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

# Re-export private helpers so existing test call sites keep working:
#   admin_read_repository._redact_message(...)
#   admin_read_repository._safe_social_attempt_metadata(...)
from app.infrastructure.persistence.repositories.admin._audit import AuditLogReadRepository
from app.infrastructure.persistence.repositories.admin._diagnostics import (
    DiagnosticsReadRepository,
)
from app.infrastructure.persistence.repositories.admin._helpers import (
    _SECRET_PATTERNS,
    _enum_value,
    _first_error,
    _parse_github_sync_state,
    _parse_since,
    _redact_match,
    _redact_message,
    _safe_social_attempt_metadata,
)
from app.infrastructure.persistence.repositories.admin._jobs import JobsReadRepository
from app.infrastructure.persistence.repositories.admin._llm import LLMReadRepository
from app.infrastructure.persistence.repositories.admin._users import UsersReadRepository

if TYPE_CHECKING:
    import datetime as dt

    from app.db.session import Database

__all__ = [
    "_SECRET_PATTERNS",
    "AdminReadRepositoryAdapter",
    "_enum_value",
    "_first_error",
    "_parse_github_sync_state",
    "_parse_since",
    "_redact_match",
    "_redact_message",
    "_safe_social_attempt_metadata",
]


class AdminReadRepositoryAdapter:
    """Read-side adapter for admin reporting queries.

    Delegates to focused bounded-context repositories; exposes the same
    public interface that all existing callers depend on.
    """

    def __init__(self, database: Database) -> None:
        self._database = database
        self._users = UsersReadRepository(database)
        self._jobs = JobsReadRepository(database)
        self._llm = LLMReadRepository(database)
        self._audit = AuditLogReadRepository(database)
        self._diagnostics = DiagnosticsReadRepository(database)

    # ------------------------------------------------------------------
    # Users / collections
    # ------------------------------------------------------------------

    async def async_list_users(self) -> dict[str, Any]:
        return await self._users.async_list_users()

    # ------------------------------------------------------------------
    # Pipeline jobs and content health
    # ------------------------------------------------------------------

    async def async_job_status(self, *, today: Any) -> dict[str, Any]:
        return await self._jobs.async_job_status(today=today)

    async def async_content_health(self) -> dict[str, Any]:
        return await self._jobs.async_content_health()

    # ------------------------------------------------------------------
    # LLM metrics and cost statistics
    # ------------------------------------------------------------------

    async def async_system_metrics(self, *, since: Any) -> dict[str, Any]:
        return await self._llm.async_system_metrics(since=since)

    async def async_llm_cost_stats(
        self, *, since: Any, today: Any, month_start: Any
    ) -> dict[str, Any]:
        return await self._llm.async_llm_cost_stats(
            since=since, today=today, month_start=month_start
        )

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    async def async_audit_log(
        self,
        *,
        action: str | None,
        user_id_filter: int | None,
        since: str | None,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        return await self._audit.async_audit_log(
            action=action,
            user_id_filter=user_id_filter,
            since=since,
            limit=limit,
            offset=offset,
        )

    # ------------------------------------------------------------------
    # Operational diagnostics snapshot
    # ------------------------------------------------------------------

    async def async_diagnostics_snapshot(
        self,
        *,
        since: dt.datetime,
        now: dt.datetime,
    ) -> dict[str, Any]:
        return await self._diagnostics.async_diagnostics_snapshot(since=since, now=now)

    # ------------------------------------------------------------------
    # Static helpers forwarded from DiagnosticsReadRepository so that
    # test call sites like:
    #   AdminReadRepositoryAdapter._llm_provider_stats(session, since=…)
    #   AdminReadRepositoryAdapter._scraper_provider_stats(session, since=…)
    # continue to resolve without modification.
    # ------------------------------------------------------------------

    _llm_provider_stats = staticmethod(DiagnosticsReadRepository._llm_provider_stats)
    _scraper_provider_stats = staticmethod(DiagnosticsReadRepository._scraper_provider_stats)
