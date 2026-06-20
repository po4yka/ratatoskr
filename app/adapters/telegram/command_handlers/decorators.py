"""Decorators for command handlers.

This module provides reusable decorators that eliminate boilerplate code
from command handlers:

- track_interaction: Automatically tracks user interactions in the database
- audit_command: Handles logging and audit trail for commands
"""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from app.application.services.user_interaction_service import async_safe_update_user_interaction
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )

logger = get_logger(__name__)

P = ParamSpec("P")
T = TypeVar("T")


def track_interaction(
    response_type: str,
    *,
    error_response_type: str | None = None,
) -> Callable[
    [Callable[[Any, CommandExecutionContext], Awaitable[T]]],
    Callable[[Any, CommandExecutionContext], Awaitable[T]],
]:
    """Decorator that handles interaction tracking automatically.

    This decorator wraps command handler methods to automatically update
    user interaction records in the database after the handler completes
    (successfully or with an error).

    Args:
        response_type: The response type to record on success.
        error_response_type: The response type to record on error.
            Defaults to "{response_type}_error".

    Returns:
        A decorator function.

    Example:
        @track_interaction("welcome")
        async def handle_start(self, ctx: CommandExecutionContext) -> None:
            await ctx.response_formatter.send_welcome(ctx.message)
    """
    if error_response_type is None:
        error_response_type = f"{response_type}_error"

    def decorator(
        func: Callable[[Any, CommandExecutionContext], Awaitable[T]],
    ) -> Callable[[Any, CommandExecutionContext], Awaitable[T]]:
        @wraps(func)
        async def wrapper(self: Any, ctx: CommandExecutionContext) -> T:
            try:
                result = await func(self, ctx)

                # Track successful interaction
                if ctx.interaction_id:
                    await async_safe_update_user_interaction(
                        ctx.user_repo,
                        interaction_id=ctx.interaction_id,
                        response_sent=True,
                        response_type=response_type,
                        start_time=ctx.start_time,
                        logger_=logger,
                    )

                return result

            except Exception as exc:
                # Track failed interaction
                if ctx.interaction_id:
                    await async_safe_update_user_interaction(
                        ctx.user_repo,
                        interaction_id=ctx.interaction_id,
                        response_sent=True,
                        response_type=error_response_type,
                        error_occurred=True,
                        error_message=str(exc)[:500],
                        start_time=ctx.start_time,
                        logger_=logger,
                    )
                raise

        return wrapper

    return decorator


def audit_command(
    event_name: str,
    *,
    include_text: bool = False,
    text_max_len: int = 100,
) -> Callable[
    [Callable[[Any, CommandExecutionContext], Awaitable[T]]],
    Callable[[Any, CommandExecutionContext], Awaitable[T]],
]:
    """Decorator that handles audit logging for commands.

    This decorator logs the command execution to both the application logger
    and the audit trail (via the audit_func callback in the context).

    Args:
        event_name: The event name to log (e.g., "command_start").
        include_text: Whether to include message text in the log.
        text_max_len: Maximum length of text to include.

    Returns:
        A decorator function.

    Example:
        @audit_command("command_start")
        async def handle_start(self, ctx: CommandExecutionContext) -> None:
            await ctx.response_formatter.send_welcome(ctx.message)
    """

    def decorator(
        func: Callable[[Any, CommandExecutionContext], Awaitable[T]],
    ) -> Callable[[Any, CommandExecutionContext], Awaitable[T]]:
        @wraps(func)
        async def wrapper(self: Any, ctx: CommandExecutionContext) -> T:
            # Build log extra data
            extra: dict[str, Any] = {
                "uid": ctx.uid,
                "chat_id": ctx.chat_id,
                "cid": ctx.correlation_id,
            }
            if include_text:
                extra["text"] = ctx.text[:text_max_len]

            # Log to application logger
            logger.info(event_name, extra=extra)

            # Log to audit trail (silently fail if audit logging fails)
            try:
                ctx.audit_func("INFO", event_name, extra)
            except Exception as exc:
                raise_if_cancelled(exc)
                logger.warning("audit_log_failed", extra={"error": str(exc)})

            return await func(self, ctx)

        return wrapper

    return decorator


def combined_handler(
    event_name: str,
    response_type: str,
    *,
    include_text: bool = False,
    error_response_type: str | None = None,
) -> Callable[
    [Callable[[Any, CommandExecutionContext], Awaitable[T]]],
    Callable[[Any, CommandExecutionContext], Awaitable[T]],
]:
    """Combined decorator that applies both audit_command and track_interaction.

    This is a convenience decorator that combines the most common pattern
    of logging + interaction tracking in command handlers.

    Args:
        event_name: The event name to log.
        response_type: The response type to record on success.
        include_text: Whether to include message text in the log.
        error_response_type: The response type to record on error.

    Returns:
        A decorator function.

    Example:
        @combined_handler("command_dbinfo", "dbinfo")
        async def handle_dbinfo(self, ctx: CommandExecutionContext) -> None:
            # Handler code here
            pass
    """

    def decorator(
        func: Callable[[Any, CommandExecutionContext], Awaitable[T]],
    ) -> Callable[[Any, CommandExecutionContext], Awaitable[T]]:
        # Apply decorators in order: audit first, then track
        audited = audit_command(event_name, include_text=include_text)(func)
        return track_interaction(response_type, error_response_type=error_response_type)(audited)

    return decorator
