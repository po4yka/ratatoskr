"""Error handling utilities for command handlers.

This module provides context managers and utilities for standardized
error handling in command handlers.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from app.core.logging_utils import get_logger
from app.application.services.user_interaction_service import async_safe_update_user_interaction

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from app.adapters.telegram.command_handlers.execution_context import (
        CommandExecutionContext,
    )

logger = get_logger(__name__)


@asynccontextmanager
async def command_error_handler(
    ctx: CommandExecutionContext,
    error_response_type: str,
    user_message: str,
    *,
    log_event: str | None = None,
    reraise: bool = True,
) -> AsyncIterator[None]:
    """Context manager for standardized command error handling.

    This context manager wraps command logic to provide consistent error
    handling, logging, user feedback, and interaction tracking.

    Args:
        ctx: The command execution context.
        error_response_type: Response type to record on error.
        user_message: Message to show to the user on error.
        log_event: Event name for logging (defaults to error_response_type + "_failed").
        reraise: Whether to reraise the exception after handling.

    Yields:
        Control to the wrapped code block.

    Example:
        async with command_error_handler(
            ctx,
            "dbinfo",
            "Unable to read database overview right now."
        ):
            overview = self._db.get_database_overview()
            await ctx.response_formatter.send_db_overview(ctx.message, overview)
    """
    if log_event is None:
        log_event = f"{error_response_type}_failed"

    try:
        yield
    except Exception as exc:
        # Log the exception
        logger.exception(log_event, extra={"cid": ctx.correlation_id})

        # Send error message to user
        await ctx.response_formatter.send_error_notification(
            ctx.message, "unexpected_error", ctx.correlation_id, details=user_message
        )

        # Track the failed interaction
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

        if reraise:
            raise


async def handle_command_exception(
    ctx: CommandExecutionContext,
    exc: Exception,
    error_response_type: str,
    user_message: str,
    *,
    log_event: str | None = None,
) -> None:
    """Handle a command exception with standard error processing.

    This function is for use when you need to handle exceptions manually
    (e.g., in a try/except block) rather than using the context manager.

    Args:
        ctx: The command execution context.
        exc: The exception that occurred.
        error_response_type: Response type to record.
        user_message: Message to show to the user.
        log_event: Event name for logging.
    """
    if log_event is None:
        log_event = f"{error_response_type}_failed"

    # Log the exception
    logger.exception(log_event, extra={"cid": ctx.correlation_id})

    # Send error message to user
    await ctx.response_formatter.send_error_notification(
        ctx.message, "unexpected_error", ctx.correlation_id, details=user_message
    )

    # Track the failed interaction
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
