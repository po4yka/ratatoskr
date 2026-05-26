"""Microsoft Webwright sidecar adapters.

This package wraps the Webwright HTTP sidecar (ops/docker/webwright/) so the
rest of the bot can invoke browser-agent runs without owning the upstream
repo's evolving Python API. The Path A scraper provider lives under
``app.adapters.content.scraper.webwright_provider``; this package owns the
``/browse`` command path and the future tool-call hook.
"""

from app.adapters.webwright.client import WebwrightClient, WebwrightTaskResult
from app.adapters.webwright.enricher import EnrichmentResult, WebwrightEnricher

__all__ = [
    "EnrichmentResult",
    "WebwrightClient",
    "WebwrightEnricher",
    "WebwrightTaskResult",
]
