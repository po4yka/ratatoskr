"""DI-layer API runtime accessor — thin wrapper over ``app.db.api_runtime_holder``.

``app.di.api`` and other DI-layer callers that import ``get_current_api_runtime``
from this module continue to work unchanged.
"""

from __future__ import annotations

from typing import Any

from app.db.api_runtime_holder import (
    _require_api_runtime,
    clear_current_api_runtime,
    set_current_api_runtime,
)


def get_current_api_runtime() -> Any:
    """Return the active API runtime, raising RuntimeError if not initialised."""
    return _require_api_runtime()


__all__ = [
    "clear_current_api_runtime",
    "get_current_api_runtime",
    "set_current_api_runtime",
]
