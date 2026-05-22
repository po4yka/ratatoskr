# ruff: noqa: TC001
"""Route dataclasses for Telegram command dispatch."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import AliasCommandHandler, TextCommandHandler, UidCommandHandler


@dataclass(frozen=True, slots=True)
class UidCommandRoute:
    prefix: str
    handler: UidCommandHandler


@dataclass(frozen=True, slots=True)
class TextCommandRoute:
    prefix: str
    handler: TextCommandHandler


@dataclass(frozen=True, slots=True)
class AliasCommandRoute:
    aliases: tuple[str, ...]
    handler: AliasCommandHandler


@dataclass(frozen=True, slots=True)
class TelegramCommandRoutes:
    pre_alias_uid: tuple[UidCommandRoute, ...]
    pre_alias_text: tuple[TextCommandRoute, ...]
    local_search_aliases: tuple[AliasCommandRoute, ...]
    online_search_aliases: tuple[AliasCommandRoute, ...]
    pre_summarize_text: tuple[TextCommandRoute, ...]
    summarize_prefix: str
    post_summarize_uid: tuple[UidCommandRoute, ...]
    post_summarize_text: tuple[TextCommandRoute, ...]
    tail_uid: tuple[UidCommandRoute, ...]


@dataclass(frozen=True, slots=True)
class TelegramCommandContribution:
    """Self-contained contribution to the Telegram command routing table."""

    name: str
    pre_alias_uid: tuple[UidCommandRoute, ...] = ()
    pre_alias_text: tuple[TextCommandRoute, ...] = ()
    local_search_aliases: tuple[AliasCommandRoute, ...] = ()
    online_search_aliases: tuple[AliasCommandRoute, ...] = ()
    pre_summarize_text: tuple[TextCommandRoute, ...] = ()
    summarize_prefix: str | None = None
    post_summarize_uid: tuple[UidCommandRoute, ...] = ()
    post_summarize_text: tuple[TextCommandRoute, ...] = ()
    tail_uid: tuple[UidCommandRoute, ...] = ()


class TelegramCommandContributionProvider(Protocol):
    """Protocol for command groups that expose route contributions."""

    def command_contribution(self) -> TelegramCommandContribution:
        """Return this group's route contribution."""


def merge_command_contributions(
    contributions: tuple[TelegramCommandContribution, ...],
) -> TelegramCommandRoutes:
    """Merge route contributions while preserving declaration order."""
    summarize_prefixes = [
        contribution.summarize_prefix
        for contribution in contributions
        if contribution.summarize_prefix is not None
    ]
    summarize_prefix = summarize_prefixes[0] if summarize_prefixes else "/summarize"
    if len(set(summarize_prefixes)) > 1:
        msg = "Conflicting summarize command prefixes"
        raise ValueError(msg)

    return TelegramCommandRoutes(
        pre_alias_uid=tuple(
            route for contribution in contributions for route in contribution.pre_alias_uid
        ),
        pre_alias_text=tuple(
            route for contribution in contributions for route in contribution.pre_alias_text
        ),
        local_search_aliases=tuple(
            route for contribution in contributions for route in contribution.local_search_aliases
        ),
        online_search_aliases=tuple(
            route for contribution in contributions for route in contribution.online_search_aliases
        ),
        pre_summarize_text=tuple(
            route for contribution in contributions for route in contribution.pre_summarize_text
        ),
        summarize_prefix=summarize_prefix,
        post_summarize_uid=tuple(
            route for contribution in contributions for route in contribution.post_summarize_uid
        ),
        post_summarize_text=tuple(
            route for contribution in contributions for route in contribution.post_summarize_text
        ),
        tail_uid=tuple(route for contribution in contributions for route in contribution.tail_uid),
    )
