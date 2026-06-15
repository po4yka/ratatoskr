"""Content extraction port (ADR-0015).

One port whose single adapter (landed by T7) dispatches by source kind to the
multi-provider scraper chain OR a platform-specific extractor, replacing the
``url_processor`` / ``ContentExtractor`` indirection with one seam the graph
``extract`` node calls.

References (does not duplicate) the concrete contracts it will fuse:
``app.adapters.content.scraper.protocol.ContentScraperProtocol`` (the chain)
and ``app.adapters.content.platform_extraction.protocol.PlatformExtractor``
(youtube / twitter / academic / github / meta). Those modules live in the
adapter layer and are intentionally NOT imported here -- the port must stay
adapter-free (``application-no-outward``). The serializable
``ExtractionRequest`` / ``ExtractionResult`` DTOs (no Telegram objects) land
with T7; T3 scaffolds the port surface only, so ``request`` / result stay
untyped here.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExtractionPort(Protocol):
    """Extract source content for a request via the chain or a platform extractor."""

    async def extract(self, request: Any) -> Any:
        """Return the extraction result for ``request`` (typed DTOs land in T7)."""
        ...
