"""Owner reaction -> summary feedback recording."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.adapters.telegram.reaction_feedback import ReactionFeedbackHandler


def _reaction(emoji: str | None, message_id: int | None = 42) -> SimpleNamespace:
    return SimpleNamespace(emoji=emoji, message_id=message_id, chat_id=1)


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
