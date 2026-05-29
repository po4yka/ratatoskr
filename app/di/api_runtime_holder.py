"""Process-wide API runtime holder, deliberately free of any ``app.api`` import.

Split out of ``app.di.api`` (which imports ``app.api.*``) so that layers which
must not depend on the API — notably infrastructure persistence helpers resolving
the runtime ``Database`` in both the API process and the worker/bot processes —
can read the active runtime without pulling ``app.api`` in transitively.

``app.di.api`` re-exports these names, so existing
``from app.di.api import get_current_api_runtime`` call sites keep working. The
runtime is typed ``Any`` here on purpose: importing the concrete ``ApiRuntime``
type (even under ``TYPE_CHECKING``) would re-introduce the ``app.api`` dependency
this module exists to avoid.
"""

from __future__ import annotations

from typing import Any

# Single-slot holder for the process-wide API runtime (None until initialized).
_current_runtime_holder: list[Any] = [None]


def get_current_api_runtime() -> Any:
    """Return the active API runtime, requiring explicit initialization."""
    if _current_runtime_holder[0] is None:
        msg = "API runtime is not initialized"
        raise RuntimeError(msg)
    return _current_runtime_holder[0]


def set_current_api_runtime(runtime: Any) -> None:
    _current_runtime_holder[0] = runtime


def clear_current_api_runtime() -> None:
    """Clear the process-wide API runtime (call during shutdown)."""
    _current_runtime_holder[0] = None
