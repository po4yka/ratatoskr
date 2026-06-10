"""Startup validator for the Telethon userbot SQLite session schema.

The canonical implementation lives in ``app/core/telethon_session``; this
module re-exports it for backward compatibility within the ``digest`` adapter.
"""

from __future__ import annotations

from app.core.telethon_session import validate_and_repair_session

__all__ = ["validate_and_repair_session"]
