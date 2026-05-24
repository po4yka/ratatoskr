"""Deterministic UUID helpers shared across the application and infrastructure layers."""

from __future__ import annotations

import uuid

_UUID_NAMESPACE = uuid.NAMESPACE_OID


def str_to_uuid(value: str) -> str:
    """Hash an arbitrary string to a deterministic UUID string."""
    return str(uuid.uuid5(_UUID_NAMESPACE, value))
