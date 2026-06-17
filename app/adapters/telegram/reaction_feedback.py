"""Map an owner reaction on a summary message to one-tap +1/-1 feedback.

Complements the existing inline +1/-1 rate buttons with a lower-friction
gesture: a thumbs reaction on a bot-sent summary is recorded as feedback.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger

logger = get_logger(__name__)

# Reaction emoji -> rating. Kept minimal: a thumbs gesture is the unambiguous
# feedback signal; any other reaction is ignored.
_EMOJI_RATING: dict[str, int] = {"👍": 1, "👎": -1}


class SummaryFeedbackRepo(Protocol):
    """The summary-repository surface the recorder needs."""

    async def async_get_summary_id_by_bot_reply(
        self, user_id: int, message_id: int
    ) -> int | None: ...

    async def async_upsert_feedback(
        self,
        user_id: int,
        summary_id: int,
        rating: int | None,
        issues: list[str] | None,
        comment: str | None,
    ) -> dict[str, Any]: ...


class ReactionFeedbackHandler:
    """Persist the owner's thumbs-up/down reaction on a summary as feedback."""

    def __init__(self, summary_repo: SummaryFeedbackRepo, owner_user_id: int) -> None:
        self._repo = summary_repo
        self._owner = owner_user_id

    async def handle(self, reaction: Any) -> None:
        """Resolve the reacted summary and upsert feedback. Best-effort."""
        rating = _EMOJI_RATING.get(reaction.emoji or "")
        if rating is None or reaction.message_id is None:
            return
        try:
            summary_id = await self._repo.async_get_summary_id_by_bot_reply(
                self._owner, reaction.message_id
            )
            if summary_id is None:
                return
            await self._repo.async_upsert_feedback(self._owner, summary_id, rating, None, None)
            logger.debug(
                "reaction_feedback_recorded",
                extra={"summary_id": summary_id, "rating": rating},
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.debug("reaction_feedback_failed", extra={"error": str(exc)})
