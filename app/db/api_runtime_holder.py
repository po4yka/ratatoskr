"""Process-wide API runtime holder, deliberately free of any ``app.api`` import.

Infrastructure persistence helpers that resolve the runtime ``Database`` in both
the API process and the worker/bot processes can import from this module without
pulling in ``app.api`` or the DI layer transitively.

The DI layer re-exports these names so existing call sites there keep working.
The runtime is typed ``Any`` on purpose: importing the concrete ``ApiRuntime``
type (even under ``TYPE_CHECKING``) would re-introduce the ``app.api`` dependency
this module exists to avoid.
"""

from __future__ import annotations

from typing import Any

# Single-slot holder for the process-wide API runtime (None until initialized).
_current_runtime_holder: list[Any] = [None]


def _read_api_runtime() -> Any:
    """Return the active API runtime slot value (``None`` if not initialised)."""
    return _current_runtime_holder[0]


def _require_api_runtime() -> Any:
    """Return the active API runtime, raising if not yet initialised."""
    runtime = _current_runtime_holder[0]
    if runtime is None:
        msg = "API runtime is not initialized"
        raise RuntimeError(msg)
    return runtime


def set_current_api_runtime(runtime: Any) -> None:
    _current_runtime_holder[0] = runtime


def clear_current_api_runtime() -> None:
    """Clear the process-wide API runtime (call during shutdown)."""
    _current_runtime_holder[0] = None
