"""Contract for star-list suggestion.

Kept beside :mod:`app.core.repo_analysis_schema` so the classifier service, the
suggestion use case, and the ports that connect them all depend on the same
declaration rather than on each other.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class StarListCandidate(BaseModel):
    """One of the user's live star lists, offered as a choice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=500)


class StarListChoice(BaseModel):
    """A pick from the offered candidates.

    An empty ``list_name`` is a valid answer meaning "none of these fit". It is
    preferred over a guess: GitHub caps a user at 32 lists, so a name that is not
    on the offered list cannot be created, and writing one would clear the
    repository's membership instead of setting it.
    """

    model_config = ConfigDict(extra="forbid")

    list_name: str = Field(default="", max_length=200)
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = Field(default="", max_length=500)
