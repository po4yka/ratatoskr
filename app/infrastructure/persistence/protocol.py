"""Protocol aliases for database executors used by repository adapters."""

from __future__ import annotations

from app.db.runtime.protocol import DatabaseExecutorPort

DatabaseSessionProtocol = DatabaseExecutorPort

__all__ = ["DatabaseExecutorPort", "DatabaseSessionProtocol"]
