"""Support types and helpers for Telegram command dispatch."""

from .context import CommandContextFactory
from .executor import (
    dispatch_alias_routes,
    dispatch_summarize_fallback,
    dispatch_text_routes,
    dispatch_uid_routes,
)
from .models import (
    AliasCommandHandler,
    CommandDispatchOutcome,
    SummarizeCommandHandler,
    TextCommandHandler,
    UidCommandHandler,
)
from .routes import (
    AliasCommandRoute,
    TelegramCommandContribution,
    TelegramCommandContributionProvider,
    TelegramCommandRoutes,
    TextCommandRoute,
    UidCommandRoute,
    merge_command_contributions,
)
from .state import TelegramCommandRuntimeState

__all__ = [
    "AliasCommandHandler",
    "AliasCommandRoute",
    "CommandContextFactory",
    "CommandDispatchOutcome",
    "SummarizeCommandHandler",
    "TelegramCommandContribution",
    "TelegramCommandContributionProvider",
    "TelegramCommandRoutes",
    "TelegramCommandRuntimeState",
    "TextCommandHandler",
    "TextCommandRoute",
    "UidCommandHandler",
    "UidCommandRoute",
    "dispatch_alias_routes",
    "dispatch_summarize_fallback",
    "dispatch_text_routes",
    "dispatch_uid_routes",
    "merge_command_contributions",
]
