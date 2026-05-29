"""Admin read queries for operational diagnostics snapshots.

Delegates individual query groups to:
  _diagnostics_providers  -- LLM/scraper provider stats, vector lag, queue backlog,
                             integration health, storage activity
  _diagnostics_sync       -- social connection diagnostics, latest sync failures
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from app.infrastructure.persistence.repositories.admin._diagnostics_providers import (
    _integration_health,
    _llm_provider_stats,
    _queue_backlog,
    _scraper_provider_stats,
    _storage_activity,
    _vector_indexing_lag,
)
from app.infrastructure.persistence.repositories.admin._diagnostics_sync import (
    _latest_sync_failures,
    _social_connection_diagnostics,
)

if TYPE_CHECKING:
    from app.db.session import Database


class DiagnosticsReadRepository:
    """Read-side queries for operational diagnostics."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_diagnostics_snapshot(
        self,
        *,
        since: dt.datetime,
        now: dt.datetime,
    ) -> dict[str, Any]:
        """Return redacted operational diagnostics from persisted state."""
        async with self._database.session() as session:
            return {
                "queue_backlog": await _queue_backlog(session, now=now),
                "vector_indexing_lag": await _vector_indexing_lag(session),
                "llm_providers": await _llm_provider_stats(session, since=since),
                "scraper_providers": await _scraper_provider_stats(session, since=since),
                "social_connections": await _social_connection_diagnostics(session, since=since),
                "integration_health": await _integration_health(session),
                "latest_sync_failures": await _latest_sync_failures(session, limit=20),
                "storage_activity": await _storage_activity(
                    session,
                    last_24h=now - dt.timedelta(days=1),
                    last_7d=now - dt.timedelta(days=7),
                ),
            }

    # ------------------------------------------------------------------
    # Expose provider-stat helpers as static methods so the facade class
    # can re-attach them and the test call sites keep working:
    #   AdminReadRepositoryAdapter._llm_provider_stats(session, since=…)
    #   AdminReadRepositoryAdapter._scraper_provider_stats(session, since=…)
    # ------------------------------------------------------------------

    _llm_provider_stats = staticmethod(_llm_provider_stats)
    _scraper_provider_stats = staticmethod(_scraper_provider_stats)
