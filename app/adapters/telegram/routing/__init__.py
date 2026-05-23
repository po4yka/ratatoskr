"""Explicit collaborators for Telegram message routing."""

from app.adapters.telegram.routing.content_router import MessageContentRouter
from app.adapters.telegram.routing.context_builder import MessageRouteContextBuilder
from app.adapters.telegram.routing.failure_handler import MessageRouteFailureHandler
from app.adapters.telegram.routing.interactions import MessageInteractionRecorder
from app.adapters.telegram.routing.models import PreparedRouteContext
from app.adapters.telegram.routing.rate_limit import MessageRateLimitCoordinator
from app.adapters.telegram.routing.voice_message_processor import (
    VoiceMessageProcessor,
    has_transcribable_voice_media,
)

__all__ = [
    "MessageContentRouter",
    "MessageInteractionRecorder",
    "MessageRateLimitCoordinator",
    "MessageRouteContextBuilder",
    "MessageRouteFailureHandler",
    "PreparedRouteContext",
    "VoiceMessageProcessor",
    "has_transcribable_voice_media",
]
