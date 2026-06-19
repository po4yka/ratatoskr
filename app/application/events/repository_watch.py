"""Repository watch domain events."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class RepositoryWatchTriggered:
    """Emitted when a watched repository's README hash or latest release tag changes."""

    user_id: int
    repository_id: int
    repository_full_name: str
    trigger: Literal["readme", "release"]
    previous_value: str | None
    current_value: str
    url: str | None = None
