"""User-account and interaction ports."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime


@runtime_checkable
class UserRepositoryPort(Protocol):
    async def async_insert_user_interaction(
        self,
        *,
        user_id: int,
        interaction_type: str,
        chat_id: int | None = None,
        message_id: int | None = None,
        command: str | None = None,
        input_text: str | None = None,
        input_url: str | None = None,
        has_forward: bool = False,
        forward_from_chat_id: int | None = None,
        forward_from_chat_title: str | None = None,
        forward_from_message_id: int | None = None,
        media_type: str | None = None,
        correlation_id: str | None = None,
        structured_output_enabled: bool = False,
    ) -> int:
        """Persist a user interaction."""

    async def async_update_user_interaction(
        self,
        interaction_id: int,
        *,
        updates: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        """Update a persisted user interaction."""

    async def async_upsert_user(
        self,
        *,
        telegram_user_id: int,
        username: str | None = None,
        is_owner: bool = False,
    ) -> None:
        """Upsert a user row."""

    async def async_upsert_chat(
        self,
        *,
        chat_id: int,
        type_: str | None,
        title: str | None = None,
        username: str | None = None,
    ) -> None:
        """Upsert a chat row.

        `type_` is optional because some Telethon code paths surface a chat
        object without a `.type` attribute; the implementation must coerce
        None to a placeholder ("unknown") to satisfy the NOT NULL column.
        """

    async def async_get_user_by_telegram_id(self, telegram_user_id: int) -> dict[str, Any] | None:
        """Return user by Telegram identifier."""

    async def async_get_or_create_user(
        self,
        telegram_user_id: int,
        *,
        username: str | None = None,
        is_owner: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Return an existing user or create one."""

    async def async_set_link_nonce(
        self,
        *,
        telegram_user_id: int,
        nonce: str,
        expires_at: datetime,
    ) -> None:
        """Store a Telegram linking nonce."""

    async def async_clear_link_nonce(self, *, telegram_user_id: int) -> None:
        """Clear a Telegram linking nonce."""

    async def async_complete_telegram_link(
        self,
        *,
        telegram_user_id: int,
        linked_telegram_user_id: int,
        username: str | None,
        photo_url: str | None,
        first_name: str | None,
        last_name: str | None,
        linked_at: datetime,
    ) -> None:
        """Persist completed Telegram link metadata."""

    async def async_unlink_telegram(self, *, telegram_user_id: int) -> None:
        """Remove Telegram link metadata."""

    async def async_delete_user(self, *, telegram_user_id: int) -> None:
        """Delete a user and related data."""

    async def async_update_user_preferences(
        self,
        telegram_user_id: int,
        preferences: dict[str, Any],
    ) -> None:
        """Update user preferences."""

    async def async_update_user_profile(self, telegram_user_id: int, **values: Any) -> None:
        """Update typed user profile fields."""

    async def async_get_max_server_version(self, user_id: int) -> int | None:
        """Return the maximum server_version for the user identified by *user_id* (telegram_user_id)."""
