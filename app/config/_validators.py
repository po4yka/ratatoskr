from __future__ import annotations

import json
from typing import Any

from app.core.logging_utils import get_logger

logger = get_logger(__name__)


def validate_model_name(model: str) -> str:
    """Validate model name for security and allow OpenRouter-style IDs."""
    if not model:
        msg = "Model name cannot be empty"
        raise ValueError(msg)
    if len(model) > 100:
        msg = "Model name too long"
        raise ValueError(msg)

    if ".." in model or "<" in model or ">" in model or "\\" in model:
        msg = "Model name contains invalid characters"
        raise ValueError(msg)

    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.:/")
    if any(ch not in allowed for ch in model):
        msg = "Model name contains invalid characters"
        raise ValueError(msg)

    return model


def _ensure_api_key(value: str, *, name: str) -> str:
    if not value:
        msg = f"{name} API key is required"
        raise ValueError(msg)
    value = value.strip()
    if not value:
        msg = f"{name} API key is required"
        raise ValueError(msg)
    if len(value) > 500:
        msg = f"{name} API key appears to be too long"
        raise ValueError(msg)
    if any(char in value for char in [" ", "\n", "\t"]):
        msg = f"{name} API key contains invalid characters"
        raise ValueError(msg)
    return value


def parse_str_sequence(value: Any, *, name: str) -> Any:
    """Turn an env-supplied scalar into a list for a sequence-typed config field.

    Pydantic rejects a bare string for ``list[str]``/``tuple[str, ...]``, and a
    field without a ``mode="before"`` parser therefore fails validation the
    moment an operator sets its env var -- which aborts ``load_config()`` for the
    bot, the API and the worker alike. Every env-settable sequence field routes
    through here so setting one can never be the thing that stops the service.

    Accepts a JSON array, a comma-separated string, or an already-parsed
    sequence (the shape ratatoskr.yaml produces). Values are stripped and empty
    entries dropped; nothing else is normalized, because callers differ -- hosts
    are case-insensitive, regex patterns are not.
    """
    if value is None or value == "":
        return []
    if isinstance(value, list | tuple | set | frozenset):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                msg = f"{name} looks like a JSON array but does not parse: {exc}"
                raise ValueError(msg) from exc
            if not isinstance(decoded, list):
                msg = f"{name} JSON value must be an array"
                raise ValueError(msg)
            return [str(item).strip() for item in decoded if str(item).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]
    msg = f"{name} must be a comma-separated string, a JSON array, or a list"
    raise ValueError(msg)


def _parse_allowed_user_ids(value: Any) -> tuple[int, ...]:
    if value in (None, ""):
        return ()
    values = value if isinstance(value, list | tuple) else str(value).split(",")

    user_ids: list[int] = []
    for piece in values:
        piece = str(piece).strip()
        if not piece:
            continue
        try:
            user_ids.append(int(piece))
        except ValueError:
            logger.debug("allowed_user_id_parse_failed", extra={"value": piece})
            continue
    return tuple(user_ids)


def parse_fallback_models(value: Any) -> tuple[str, ...]:
    """Parse comma-separated/list fallback model names with validation."""
    if value in (None, ""):
        return ()
    iterable = value if isinstance(value, list | tuple) else str(value).split(",")

    validated: list[str] = []
    for raw in iterable:
        candidate = str(raw).strip()
        if not candidate:
            continue
        try:
            validated.append(validate_model_name(candidate))
        except ValueError:
            logger.debug("fallback_model_validation_failed", extra={"model": candidate})
            continue
    return tuple(validated)
