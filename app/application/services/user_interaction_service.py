"""Application service for user interaction updates."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.services.user_interaction_update import (
    async_safe_update_user_interaction as _async_safe_update_user_interaction,
)

if TYPE_CHECKING:
    import logging

    from app.application.ports.users import UserRepositoryPort


class UserInteractionService:
    """Coordinates safe user-interaction persistence for presentation layers."""

    async def update(
        self,
        user_repo: UserRepositoryPort | Any,
        *,
        interaction_id: int | None,
        logger_: logging.Logger | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
        updates: dict[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        await _async_safe_update_user_interaction(
            user_repo,
            interaction_id=interaction_id,
            logger_=logger_,
            start_time=start_time,
            end_time=end_time,
            updates=updates,
            **fields,
        )


_default_user_interaction_service = UserInteractionService()


async def async_safe_update_user_interaction(
    user_repo: UserRepositoryPort | Any,
    *,
    interaction_id: int | None,
    logger_: logging.Logger | None = None,
    start_time: float | None = None,
    end_time: float | None = None,
    updates: dict[str, Any] | None = None,
    **fields: Any,
) -> None:
    """Compatibility facade for callers that have not injected the service yet."""
    await _default_user_interaction_service.update(
        user_repo,
        interaction_id=interaction_id,
        logger_=logger_,
        start_time=start_time,
        end_time=end_time,
        updates=updates,
        **fields,
    )
