"""Input validation utilities for safe type conversion and validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

logger = get_logger(__name__)

T = TypeVar("T")


def safe_cast(
    value: Any,
    target_type: type[T],
    validator: Callable[[T], bool] | None = None,
    default: T | None = None,
    field_name: str = "value",
) -> T | None:
    """Safely cast and validate a value with proper error handling.

    Args:
        value: The value to cast
        target_type: The target type to cast to
        validator: Optional validation function
        default: Default value if cast/validation fails
        field_name: Name of field for logging

    Returns:
        Casted and validated value, or default if invalid

    """
    try:
        if value is None:
            return default

        casted = target_type(value)  # type: ignore[call-arg]

        if validator and not validator(casted):
            logger.warning(
                "validation_failed",
                extra={
                    "field": field_name,
                    "value_type": type(value).__name__,
                    "target_type": target_type.__name__,
                },
            )
            return default

        return casted

    except (ValueError, TypeError, OverflowError) as e:
        logger.warning(
            "cast_failed",
            extra={
                "field": field_name,
                "value_type": type(value).__name__,
                "target_type": target_type.__name__,
                "error": str(e),
            },
        )
        return default


def safe_telegram_user_id(raw_value: Any, field_name: str = "user_id") -> int | None:
    """Safely validate and convert Telegram user ID.

    Telegram user IDs are positive 32-bit integers.

    Args:
        raw_value: Raw value to validate
        field_name: Field name for logging

    Returns:
        Valid user ID or None

    """
    return safe_cast(
        raw_value,
        int,
        validator=lambda x: 0 < x < 2**31,
        default=None,
        field_name=field_name,
    )


def safe_telegram_chat_id(raw_value: Any, field_name: str = "chat_id") -> int | None:
    """Safely validate and convert Telegram chat ID.

    Telegram chat IDs can be negative for groups/channels.

    Args:
        raw_value: Raw value to validate
        field_name: Field name for logging

    Returns:
        Valid chat ID or None

    """
    return safe_cast(
        raw_value,
        int,
        validator=lambda x: -(2**31) < x < 2**31,
        default=None,
        field_name=field_name,
    )


def safe_message_id(raw_value: Any, field_name: str = "message_id") -> int | None:
    """Safely validate and convert Telegram message ID.

    Args:
        raw_value: Raw value to validate
        field_name: Field name for logging

    Returns:
        Valid message ID or None

    """
    return safe_cast(
        raw_value,
        int,
        validator=lambda x: 0 < x < 2**63,
        default=None,
        field_name=field_name,
    )
