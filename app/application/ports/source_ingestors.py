"""Ports and normalized DTOs for proactive source ingestors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime


@dataclass(slots=True, frozen=True)
class IngestedSource:
    """Normalized source metadata emitted by an ingester."""

    kind: str
    external_id: str
    url: str | None = None
    title: str | None = None
    description: str | None = None
    site_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class IngestedFeedItem:
    """Normalized item emitted by any configured source."""

    external_id: str
    canonical_url: str | None = None
    title: str | None = None
    content_text: str | None = None
    author: str | None = None
    published_at: Any | None = None
    engagement: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class SourceFetchResult:
    """One source fetch result before persistence."""

    source: IngestedSource
    items: list[IngestedFeedItem] = field(default_factory=list)
    not_modified: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


class SourceIngestionError(RuntimeError):
    """Base class for source ingestion failures."""

    permanent: bool = False


class TransientSourceError(SourceIngestionError):
    """Temporary network/provider failure; backoff and retry later."""


class RateLimitedSourceError(TransientSourceError):
    """Provider rate limit; retry after provider/global budget recovers."""

    def __init__(self, message: str, *, retry_at: datetime | None = None) -> None:
        super().__init__(message)
        self.retry_at = retry_at


class AuthSourceError(SourceIngestionError):
    """Permanent auth or permission failure until config changes."""

    permanent = True


@runtime_checkable
class SourceIngester(Protocol):
    """Fetch and normalize one configured source into generic signal items."""

    name: str

    def is_enabled(self) -> bool:
        """Return whether this ingester should run in the current config."""

    def source_identity(self) -> IngestedSource:
        """Return persisted source identity without making a provider call."""

    async def fetch(self) -> SourceFetchResult:
        """Fetch, normalize, and return items for one source."""


@dataclass(slots=True, frozen=True)
class SourceIngesterBuildContext:
    """Optional dependencies available to registry-built ingestors."""

    social_connection_repository: Any | None = None
    social_token_resolver: Any | None = None
    subscriber_user_ids: tuple[int, ...] = ()
    x_api_base_url: str = "https://api.x.com/2"
    threads_graph_base_url: str = "https://graph.threads.net/v1.0"


@dataclass(slots=True, frozen=True)
class SourceIngesterDescriptor:
    """Static registry entry that builds one proactive source ingester family."""

    name: str
    build: Callable[[Any, SourceIngesterBuildContext], Iterable[SourceIngester]]
