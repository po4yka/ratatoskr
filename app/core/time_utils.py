from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:  # Python 3.11+
    from datetime import UTC
except ImportError:  # pragma: no cover - Python < 3.11
    UTC = timezone.utc  # noqa: UP017

__all__ = ["UTC", "coerce_datetime", "format_iso_z", "isotime", "utc_now"]


def utc_now() -> datetime:
    """Return a timezone-aware UTC datetime."""
    return datetime.now(UTC)


def coerce_datetime(raw: object) -> datetime:
    """Coerce a raw value to a timezone-aware UTC datetime.

    Accepts datetime objects (returned as-is) or ISO8601 strings.
    Falls back to utc_now() on parse failure or unknown type.
    """
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now(UTC)


def format_iso_z(value: datetime) -> str:
    """Format a datetime as UTC ISO8601 with a trailing Z suffix."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def isotime(dt_val: Any) -> str:
    """Convert a datetime to an ISO string with a Z suffix.

    Lenient by design (used across API serialization helpers): returns "" for
    None and falls back to str() for non-datetime values.
    """
    if dt_val is None:
        return ""
    if isinstance(dt_val, datetime):
        return format_iso_z(dt_val)
    if hasattr(dt_val, "isoformat"):
        return str(dt_val.isoformat()) + "Z"
    return str(dt_val)
