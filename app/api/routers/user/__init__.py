"""User profile route handlers: user account, highlights, tags, feeds, and text-to-speech."""

from . import feed, highlights, tags, tts, user
from .user import (
    complete_onboarding,
    get_current_user_profile,
    get_user_preferences,
    profile_router,
    safe_isoformat,
    update_current_user_profile,
)

profile_router.include_router(feed.router)

__all__ = [
    "complete_onboarding",
    "feed",
    "get_current_user_profile",
    "get_user_preferences",
    "highlights",
    "profile_router",
    "safe_isoformat",
    "tags",
    "tts",
    "update_current_user_profile",
    "user",
]
