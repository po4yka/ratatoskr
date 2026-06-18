"""Content extraction port (ADR-0015).

One port whose single adapter (``app.adapters.content.extraction_adapter``)
dispatches by source kind to the multi-provider scraper chain OR a
platform-specific extractor (youtube / twitter / academic / github / meta),
replacing the ``url_processor`` / ``ContentExtractor`` indirection with one seam
the graph ``extract`` node calls.

The ``ExtractionRequest`` / ``ExtractionResult`` DTOs are deliberately
**serializable, primitive-only, and Telegram-free** (ADR-0011/0015): the extract
node lifts only ids/handles out of the result into ``SummarizeState`` and
re-fetches bulk content downstream, and notification concerns flow through the
notify node + ``StreamSinkPort``, never through these DTOs.

The port references (does not import) the concrete contracts its adapter fuses --
``app.adapters.content.scraper.protocol.ContentScraperProtocol`` (the chain) and
``app.adapters.content.platform_extraction.protocol.PlatformExtractor`` -- which
live in the adapter layer and must NOT be imported here (``application-no-outward``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ExtractionRequest:
    """Serializable request for one extraction (no Telegram objects).

    ``request_id`` is the already-created request row the extraction is attached
    to (crawl results / failures persist against it); ``url`` is the raw input
    URL (the adapter normalizes it via ``app.core.url_utils`` before dispatch).
    """

    url: str
    request_id: int | None = None
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Serializable extraction outcome the ``extract`` node projects into state.

    Mirrors the load-bearing fields of the legacy ``PlatformExtractionResult`` /
    ``ContentExtractionResult`` without any Telegram/live objects: the node lifts
    ``content_source`` / ``detected_lang`` / ``title`` / ``dedupe_hash`` into the
    minimal id-based ``SummarizeState`` and hands ``content_text`` to the
    ground/summarize nodes via ``state['source_text']``.
    """

    request_id: int | None
    content_text: str
    content_source: str
    detected_lang: str
    dedupe_hash: str
    title: str | None = None
    canonical_url: str | None = None
    images: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ExtractionPort(Protocol):
    """Extract source content for a request via the chain or a platform extractor."""

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """Return the extraction result for ``request``.

        Raises ``ValueError`` (or a subclass) on an extraction failure that was
        already persisted via ``persist_request_failure`` -- the ``extract`` node
        lets it propagate to the single terminal-failure path (ADR-0011).
        """
        ...
