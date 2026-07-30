"""Owner reaction -> summary feedback recording."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.adapters.telegram.reaction_feedback import ReactionFeedbackHandler


def _reaction(
    emoji: str | None,
    message_id: int | None = 42,
    actor_id: int | None = 100,
) -> SimpleNamespace:
    return SimpleNamespace(emoji=emoji, message_id=message_id, chat_id=1, actor_id=actor_id)


async def test_thumbs_up_records_positive_feedback() -> None:
    repo = AsyncMock()
    repo.async_get_summary_id_by_bot_reply.return_value = 7
    await ReactionFeedbackHandler(repo, owner_user_id=100).handle(_reaction("👍"))
    repo.async_get_summary_id_by_bot_reply.assert_awaited_once_with(100, 42)
    repo.async_upsert_feedback.assert_awaited_once_with(100, 7, 1, None, None)


async def test_thumbs_down_records_negative_feedback() -> None:
    repo = AsyncMock()
    repo.async_get_summary_id_by_bot_reply.return_value = 9
    await ReactionFeedbackHandler(repo, 100).handle(_reaction("👎"))
    repo.async_upsert_feedback.assert_awaited_once_with(100, 9, -1, None, None)


async def test_unknown_emoji_is_ignored() -> None:
    repo = AsyncMock()
    await ReactionFeedbackHandler(repo, 100).handle(_reaction("🔥"))
    repo.async_get_summary_id_by_bot_reply.assert_not_awaited()
    repo.async_upsert_feedback.assert_not_awaited()


async def test_no_matching_summary_skips_upsert() -> None:
    repo = AsyncMock()
    repo.async_get_summary_id_by_bot_reply.return_value = None
    await ReactionFeedbackHandler(repo, 100).handle(_reaction("👍"))
    repo.async_upsert_feedback.assert_not_awaited()


async def test_stranger_reaction_is_not_recorded_as_owner_feedback() -> None:
    """A non-owner's thumbs must never touch the owner's summary.

    This handler sits on the raw update stream, outside MessageRouter, so it is
    the one entrypoint AccessController does not cover. A non-allowlisted user
    gets an "access denied" reply and can react to it; message ids are per-chat,
    so that id can collide with a bot_reply_message_id of the owner's.
    """
    repo = AsyncMock()
    repo.async_get_summary_id_by_bot_reply.return_value = 7

    await ReactionFeedbackHandler(repo, owner_user_id=100).handle(_reaction("👍", actor_id=666))

    repo.async_get_summary_id_by_bot_reply.assert_not_awaited()
    repo.async_upsert_feedback.assert_not_awaited()


async def test_unresolvable_actor_fails_closed() -> None:
    """No actor on the update means no feedback write."""
    repo = AsyncMock()
    repo.async_get_summary_id_by_bot_reply.return_value = 7

    await ReactionFeedbackHandler(repo, owner_user_id=100).handle(_reaction("👍", actor_id=None))

    repo.async_upsert_feedback.assert_not_awaited()
