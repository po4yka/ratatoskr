"""Lazy Telethon import block shared by all compat submodules.

Telethon may be absent in the minimal test environment (CI without the ``graph``
extra). Every submodule that needs the telethon namespace imports from here so
the guard lives in exactly one place.
"""

from __future__ import annotations

TELETHON_AVAILABLE = True
try:  # pragma: no cover - exercised when dependency is installed
    from telethon import Button, TelegramClient, events, functions, types, utils
    from telethon.errors import SessionPasswordNeededError as _SessionPasswordNeededError
except Exception:  # pragma: no cover - allow import in minimal test envs
    Button = None
    TelegramClient = None
    events = None
    functions = None
    types = None
    utils = None

    class _SessionPasswordNeededError(Exception):  # type: ignore[no-redef]
        """Fallback exception used when Telethon is unavailable."""

    TELETHON_AVAILABLE = False

SessionPasswordNeededError = _SessionPasswordNeededError

__all__ = [
    "TELETHON_AVAILABLE",
    "Button",
    "SessionPasswordNeededError",
    "TelegramClient",
    "_SessionPasswordNeededError",
    "events",
    "functions",
    "types",
    "utils",
]
