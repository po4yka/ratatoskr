"""Consolidated JSON utilities for database operations.

This module provides utilities for:
- Preparing JSON payloads for storage
- Normalizing container types for JSON serialization
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any


def normalize_json_container(value: Any) -> Any:
    """Normalize a container (dict/list) to standard types for JSON serialization.

    Converts Mapping subclasses to dict and Sequence subclasses to list.

    Args:
        value: The container to normalize

    Returns:
        Normalized container (dict, list, or original value if not a container)

    Examples:
        >>> from collections import OrderedDict
        >>> normalize_json_container(OrderedDict([('a', 1)]))
        {'a': 1}

        >>> normalize_json_container("string")
        'string'
    """
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return list(value)
    return value


def prepare_json_payload(value: Any, *, default: Any | None = None) -> Any | None:
    """Prepare a value for storage as a JSON field.

    Handles bytes decoding, string parsing, and normalization to ensure
    the value can be safely stored in a JSON column.

    Args:
        value: The value to prepare
        default: Default value if input is None

    Returns:
        Prepared value ready for database storage, or None

    Examples:
        >>> prepare_json_payload({"key": "value"})
        {'key': 'value'}

        >>> prepare_json_payload(None, default={})
        {}

        >>> prepare_json_payload(b'{"key": "value"}')
        {'key': 'value'}
    """
    if value is None:
        value = default
    if value is None:
        return None

    # Handle memoryview
    if isinstance(value, memoryview):
        value = value.tobytes()

    # Handle bytes
    if isinstance(value, bytes | bytearray):
        try:
            value = value.decode("utf-8")
        except (UnicodeDecodeError, AttributeError):
            value = value.decode("utf-8", errors="replace")

    # Handle string input - try to parse as JSON
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return stripped

    # Normalize containers
    normalized = normalize_json_container(value)

    # Verify it's JSON-serializable
    try:
        json.dumps(normalized)
        return normalized
    except (TypeError, ValueError):
        # Try coercing with default=str
        try:
            coerced = json.loads(json.dumps(normalized, default=str))
        except (TypeError, ValueError):
            return None
        return coerced
