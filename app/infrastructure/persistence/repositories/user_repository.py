"""SQLAlchemy implementation of user, chat, and interaction repositories."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Chat, User, UserInteraction, model_to_dict
from app.db.types import _utcnow

if TYPE_CHECKING:
    import datetime as dt
    from collections.abc import Mapping

    from app.db.session import Database


class UserRepositoryAdapter:
    """Adapter for user and interaction operations using SQLAlchemy."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def async_get_max_server_version(self, user_id: int) -> int | None:
        """Return the maximum server_version for the user identified by *user_id*."""
        async with self._database.session() as session:
            value = await session.scalar(
                select(func.max(User.server_version)).where(User.telegram_user_id == user_id)
            )
            return int(value) if value is not None else None

    async def async_get_user_by_telegram_id(self, telegram_user_id: int) -> dict[str, Any] | None:
        """Get a user by Telegram user ID."""
        async with self._database.session() as session:
            user = await session.get(User, telegram_user_id)
            return model_to_dict(user)

    async def async_get_or_create_user(
        self,
        telegram_user_id: int,
        *,
        username: str | None = None,
        is_owner: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        """Get or create a user by Telegram ID."""
        async with self._database.transaction() as session:
            stmt = (
                insert(User)
                .values(
                    telegram_user_id=telegram_user_id,
                    username=username,
                    is_owner=is_owner,
                )
                .on_conflict_do_nothing(index_elements=[User.telegram_user_id])
                .returning(User)
            )
            inserted = await session.scalar(stmt)
            if inserted is not None:
                return model_to_dict(inserted) or {}, True
            user = await session.get(User, telegram_user_id)
            return model_to_dict(user) or {}, False

    async def async_set_link_nonce(
        self,
        *,
        telegram_user_id: int,
        nonce: str,
        expires_at: dt.datetime,
    ) -> None:
        """Set link nonce fields for a user."""
        await self._update_user(
            telegram_user_id,
            link_nonce=nonce,
            link_nonce_expires_at=expires_at,
        )

    async def async_clear_link_nonce(self, *, telegram_user_id: int) -> None:
        """Clear link nonce fields for a user."""
        await self._update_user(
            telegram_user_id,
            link_nonce=None,
            link_nonce_expires_at=None,
        )

    async def async_complete_telegram_link(
        self,
        *,
        telegram_user_id: int,
        linked_telegram_user_id: int,
        username: str | None,
        photo_url: str | None,
        first_name: str | None,
        last_name: str | None,
        linked_at: dt.datetime,
    ) -> None:
        """Complete Telegram account linking for a user."""
        await self._update_user(
            telegram_user_id,
            linked_telegram_user_id=linked_telegram_user_id,
            linked_telegram_username=username,
            linked_telegram_photo_url=photo_url,
            linked_telegram_first_name=first_name,
            linked_telegram_last_name=last_name,
            linked_at=linked_at,
            link_nonce=None,
            link_nonce_expires_at=None,
        )

    async def async_unlink_telegram(self, *, telegram_user_id: int) -> None:
        """Remove Telegram link information from a user."""
        await self._update_user(
            telegram_user_id,
            linked_telegram_user_id=None,
            linked_telegram_username=None,
            linked_telegram_photo_url=None,
            linked_telegram_first_name=None,
            linked_telegram_last_name=None,
            linked_at=None,
            link_nonce=None,
            link_nonce_expires_at=None,
        )

    async def async_delete_user(self, *, telegram_user_id: int) -> None:
        """Delete a user and related data."""
        async with self._database.transaction() as session:
            await session.execute(delete(User).where(User.telegram_user_id == telegram_user_id))

    async def async_update_user_preferences(
        self, telegram_user_id: int, preferences: dict[str, Any]
    ) -> None:
        """Update user preferences."""
        await self._update_user(telegram_user_id, preferences_json=preferences)

    async def async_update_user_profile(self, telegram_user_id: int, **values: Any) -> None:
        """Update typed user profile fields."""
        allowed = {
            "onboarding_completed_at",
            "locale",
            "theme",
            "display_name",
            "default_summary_language",
        }
        filtered = {key: value for key, value in values.items() if key in allowed}
        if filtered:
            await self._update_user(telegram_user_id, **filtered)

    async def async_upsert_user(
        self, *, telegram_user_id: int, username: str | None = None, is_owner: bool = False
    ) -> None:
        """Upsert a user record."""
        async with self._database.transaction() as session:
            stmt = (
                insert(User)
                .values(
                    telegram_user_id=telegram_user_id,
                    username=username,
                    is_owner=is_owner,
                )
                .on_conflict_do_update(
                    index_elements=[User.telegram_user_id],
                    set_={"username": username, "is_owner": is_owner, "updated_at": _utcnow()},
                )
            )
            await session.execute(stmt)

    async def async_upsert_chat(
        self,
        *,
        chat_id: int,
        type_: str | None,
        title: str | None = None,
        username: str | None = None,
    ) -> None:
        """Upsert a chat record.

        `type_` may be None when the caller cannot determine the chat type
        (e.g. raw Telethon events that do not expose `.type`). The column is
        NOT NULL, so a missing value is persisted as `"unknown"` on first
        insert and is *not* overwritten on conflict — that way a later
        message with a real type sticks, but a None from a degraded code
        path does not blank out a previously-known type.
        """
        insert_type = type_ if type_ else "unknown"
        update_fields: dict[str, Any] = {
            "title": title,
            "username": username,
            "updated_at": _utcnow(),
        }
        if type_:
            update_fields["type"] = type_
        async with self._database.transaction() as session:
            stmt = (
                insert(Chat)
                .values(
                    chat_id=chat_id,
                    type=insert_type,
                    title=title,
                    username=username,
                )
                .on_conflict_do_update(
                    index_elements=[Chat.chat_id],
                    set_=update_fields,
                )
            )
            await session.execute(stmt)

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
        """Insert a user interaction record."""
        async with self._database.transaction() as session:
            interaction = UserInteraction(
                user_id=user_id,
                chat_id=chat_id,
                message_id=message_id,
                interaction_type=interaction_type,
                command=command,
                input_text=input_text,
                input_url=input_url,
                has_forward=has_forward,
                forward_from_chat_id=forward_from_chat_id,
                forward_from_chat_title=forward_from_chat_title,
                forward_from_message_id=forward_from_message_id,
                media_type=media_type,
                correlation_id=correlation_id,
                structured_output_enabled=structured_output_enabled,
            )
            session.add(interaction)
            await session.flush()
            return interaction.id

    async def async_update_user_interaction(
        self,
        interaction_id: int,
        *,
        updates: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> None:
        """Update a user interaction record."""
        all_updates = dict(updates) if updates else {}
        all_updates.update(fields)
        if not all_updates:
            return

        column_names = set(UserInteraction.__mapper__.columns.keys())
        update_values = {key: value for key, value in all_updates.items() if key in column_names}
        if not update_values:
            return
        update_values["updated_at"] = _utcnow()

        async with self._database.transaction() as session:
            await session.execute(
                update(UserInteraction)
                .where(UserInteraction.id == interaction_id)
                .values(**update_values)
            )

    async def async_get_user_interactions(
        self,
        *,
        uid: int,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Get recent user interactions."""
        async with self._database.session() as session:
            rows = (
                await session.execute(
                    select(UserInteraction)
                    .where(UserInteraction.user_id == uid)
                    .order_by(UserInteraction.created_at.desc())
                    .limit(limit)
                )
            ).scalars()
            return [model_to_dict(row) or {} for row in rows]

    async def _update_user(self, telegram_user_id: int, **values: Any) -> None:
        values["updated_at"] = _utcnow()
        async with self._database.transaction() as session:
            await session.execute(
                update(User).where(User.telegram_user_id == telegram_user_id).values(**values)
            )
