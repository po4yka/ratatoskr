"""Shared utilities for command handlers."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def maybe_load_json(payload: Any) -> Any:
    """Return parsed JSON dict from payload (dict, bytes, str, or None)."""
    if payload is None:
        return None

    if isinstance(payload, Mapping):
        return dict(payload)

    if isinstance(payload, bytes | bytearray):
        try:
            payload = payload.decode("utf-8")
        except Exception:
            payload = payload.decode("utf-8", errors="replace")

    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return None

    return payload
