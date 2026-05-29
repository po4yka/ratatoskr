"""Admin diagnostics: social connection health and latest sync failures."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import func, or_, select

from app.core.time_utils import UTC
from app.db.models import (
    ImportJob,
    RequestProcessingJob,
    RSSFeed,
    SocialConnection,
    SocialFetchAttempt,
    SocialFetchAttemptStatus,
    Source,
    UserGitHubIntegration,
)
from app.infrastructure.persistence.repositories.admin._helpers import (
    _enum_value,
    _first_error,
    _parse_github_sync_state,
    _redact_message,
    _safe_social_attempt_metadata,
    isotime,
)


async def _social_connection_diagnostics(
    session: Any,
    *,
    since: dt.datetime,
) -> list[dict[str, Any]]:
    connection_rows = (
        await session.execute(
            select(
                SocialConnection.provider,
                SocialConnection.status,
                func.count(SocialConnection.id),
            ).group_by(SocialConnection.provider, SocialConnection.status)
        )
    ).all()
    by_provider: dict[str, dict[str, Any]] = {}
    for provider, status, count in connection_rows:
        provider_name = _enum_value(provider)
        status_name = _enum_value(status)
        row = by_provider.setdefault(
            provider_name,
            {
                "provider": provider_name,
                "configured": False,
                "active_connection_count": 0,
                "needs_reauth_count": 0,
                "recent_fetch_failures": [],
                "rate_limit_reset_summary": None,
            },
        )
        if status_name == "active":
            row["active_connection_count"] += int(count or 0)
        if status_name == "needs_reauth":
            row["needs_reauth_count"] += int(count or 0)

    failure_rows = (
        await session.execute(
            select(SocialFetchAttempt)
            .where(
                SocialFetchAttempt.started_at >= since,
                SocialFetchAttempt.status == SocialFetchAttemptStatus.FAILED,
            )
            .order_by(SocialFetchAttempt.started_at.desc())
            .limit(20)
        )
    ).scalars()
    latest_rate_limits: dict[str, str] = {}
    for attempt in failure_rows:
        provider_name = _enum_value(attempt.provider)
        row = by_provider.setdefault(
            provider_name,
            {
                "provider": provider_name,
                "configured": False,
                "active_connection_count": 0,
                "needs_reauth_count": 0,
                "recent_fetch_failures": [],
                "rate_limit_reset_summary": None,
            },
        )
        metadata = attempt.metadata_json if isinstance(attempt.metadata_json, dict) else {}
        row["recent_fetch_failures"].append(
            {
                "provider": provider_name,
                "attempt_type": attempt.attempt_type,
                "error_code": attempt.error_code,
                "error_message": _redact_message(attempt.error_message),
                "occurred_at": attempt.finished_at or attempt.started_at,
                "source_url": attempt.source_url,
                "normalized_url": attempt.normalized_url,
                "provider_resource_id": attempt.provider_resource_id,
                "http_status": attempt.http_status,
                "auth_tier": attempt.auth_tier,
                "rate_limit_reset_at": attempt.rate_limit_reset_at,
                "correlation_id": attempt.correlation_id,
                "metadata": _safe_social_attempt_metadata(metadata),
            }
        )
        if attempt.rate_limit_reset_at is not None:
            latest_rate_limits.setdefault(provider_name, isotime(attempt.rate_limit_reset_at))

    for provider_name, reset in latest_rate_limits.items():
        by_provider[provider_name]["rate_limit_reset_summary"] = reset
    return [by_provider[key] for key in sorted(by_provider)]


async def _latest_sync_failures(session: Any, *, limit: int) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []

    rss_rows = (
        await session.execute(
            select(
                RSSFeed.id,
                RSSFeed.fetch_error_count,
                RSSFeed.last_error,
                RSSFeed.updated_at,
            )
            .where(or_(RSSFeed.fetch_error_count > 0, RSSFeed.last_error.is_not(None)))
            .order_by(RSSFeed.updated_at.desc())
            .limit(limit)
        )
    ).all()
    for feed_id, error_count, last_error, updated_at in rss_rows:
        failures.append(
            {
                "source": "rss",
                "event_id": f"rss-feed:{feed_id}",
                "error_code": "RSS_FETCH_FAILED",
                "message": _redact_message(last_error),
                "occurred_at": updated_at,
                "retryable": True,
                "details": {"fetch_error_count": int(error_count or 0)},
            }
        )

    github_rows = (
        await session.execute(
            select(
                UserGitHubIntegration.id,
                UserGitHubIntegration.status,
                UserGitHubIntegration.last_sync_cursor,
                UserGitHubIntegration.updated_at,
            )
            .where(
                or_(
                    UserGitHubIntegration.status != "active",
                    UserGitHubIntegration.last_sync_cursor.like('%"kind": "github_sync_state"%'),
                )
            )
            .order_by(UserGitHubIntegration.updated_at.desc())
            .limit(limit)
        )
    ).all()
    for integration_id, status, last_sync_cursor, updated_at in github_rows:
        state = _parse_github_sync_state(last_sync_cursor)
        message = state.get("last_error") or f"GitHub integration status is {status}"
        failures.append(
            {
                "source": "github",
                "event_id": f"github-integration:{integration_id}",
                "error_code": f"GITHUB_{str(status).upper()}",
                "message": _redact_message(str(message)),
                "occurred_at": updated_at,
                "retryable": status == "needs_reauth",
                "details": {
                    "failure_count": state.get("failure_count"),
                    "backoff_until": state.get("backoff_until"),
                }
                if state
                else {},
            }
        )

    source_rows = (
        await session.execute(
            select(
                Source.id,
                Source.kind,
                Source.fetch_error_count,
                Source.last_error,
                Source.updated_at,
                Source.is_active,
            )
            .where(or_(Source.fetch_error_count > 0, Source.last_error.is_not(None)))
            .order_by(Source.updated_at.desc())
            .limit(limit)
        )
    ).all()
    for source_id, kind, error_count, last_error, updated_at, is_active in source_rows:
        failures.append(
            {
                "source": "source",
                "event_id": f"source:{source_id}",
                "error_code": f"SOURCE_{str(kind).upper()}_FETCH_FAILED",
                "message": _redact_message(last_error),
                "occurred_at": updated_at,
                "retryable": bool(is_active),
                "details": {
                    "kind": str(kind),
                    "fetch_error_count": int(error_count or 0),
                },
            }
        )

    import_rows = (
        await session.execute(
            select(ImportJob.id, ImportJob.status, ImportJob.errors_json, ImportJob.updated_at)
            .where(ImportJob.status.in_(("failed", "error")))
            .order_by(ImportJob.updated_at.desc())
            .limit(limit)
        )
    ).all()
    for job_id, status, errors_json, updated_at in import_rows:
        failures.append(
            {
                "source": "import",
                "event_id": f"import-job:{job_id}",
                "error_code": f"IMPORT_{str(status).upper()}",
                "message": _redact_message(_first_error(errors_json)),
                "occurred_at": updated_at,
                "retryable": True,
                "details": {},
            }
        )

    job_rows = (
        await session.execute(
            select(
                RequestProcessingJob.id,
                RequestProcessingJob.request_id,
                RequestProcessingJob.correlation_id,
                RequestProcessingJob.last_error_code,
                RequestProcessingJob.last_error_message,
                RequestProcessingJob.updated_at,
                RequestProcessingJob.status,
            )
            .where(RequestProcessingJob.status.in_(("failed", "dead_letter")))
            .order_by(RequestProcessingJob.updated_at.desc())
            .limit(limit)
        )
    ).all()
    for job_id, request_id, correlation_id, error_code, message, updated_at, status in job_rows:
        failures.append(
            {
                "source": "request",
                "event_id": f"request-processing-job:{job_id}",
                "correlation_id": correlation_id,
                "error_code": error_code or f"REQUEST_JOB_{str(status).upper()}",
                "message": _redact_message(message),
                "occurred_at": updated_at,
                "retryable": status == "failed",
                "details": {"request_id": int(request_id)},
            }
        )

    return sorted(
        failures,
        key=lambda item: item.get("occurred_at") or dt.datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )[:limit]
