"""Shared helpers for authenticated social source ingestors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

from app.application.ports.source_ingestors import RateLimitedSourceError, TransientSourceError
from app.observability.metrics import record_social_rate_limit


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def rate_limit_retry_at(headers: Any, *, now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(UTC)
    reset = _header(headers, "x-rate-limit-reset")
    if reset:
        try:
            return datetime.fromtimestamp(int(reset), tz=UTC)
        except ValueError:
            pass
    retry_after = _header(headers, "retry-after")
    if retry_after:
        try:
            return now + timedelta(seconds=max(0, int(retry_after)))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC)
    return None


def raise_for_social_response(response: Any, *, provider: str) -> None:
    if response.status_code == 429:
        record_social_rate_limit(provider=provider)
        raise RateLimitedSourceError(
            f"{provider} API returned 429",
            retry_at=rate_limit_retry_at(response.headers),
        )
    if response.status_code in {401, 403}:
        from app.application.ports.source_ingestors import AuthSourceError

        raise AuthSourceError(f"{provider} API denied access: {response.status_code}")
    if response.status_code >= 400:
        raise TransientSourceError(f"{provider} API error: {response.status_code}")


def _header(headers: Any, key: str) -> str | None:
    value = None
    if hasattr(headers, "get"):
        value = headers.get(key)
    if value is None and hasattr(headers, "get"):
        value = headers.get(key.lower())
    return str(value).strip() if value not in (None, "") else None
