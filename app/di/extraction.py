"""Composition root for the content-extraction port (ADR-0010/0015).

Wires the concrete ``ContentExtractionAdapter`` (which fuses the platform router
+ scraper chain via ``ContentExtractor``) into the application ``ExtractionPort``
the summarize graph's ``extract`` node depends on. This is the only place the
graph's extraction seam touches the adapter layer; nodes receive the port via
``SummarizeDeps`` (``app.di.graphs.build_summarize_deps``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.adapters.content.extraction_adapter import ContentExtractionAdapter

if TYPE_CHECKING:
    from app.adapters.content.content_extractor import ContentExtractor
    from app.application.ports.extraction import ExtractionPort
    from app.application.ports.requests import RequestRepositoryPort


def build_extraction_port(
    *,
    content_extractor: ContentExtractor,
    request_repo: RequestRepositoryPort,
) -> ExtractionPort:
    """Construct the ``ExtractionPort`` from an already-built ``ContentExtractor``."""
    return ContentExtractionAdapter(
        content_extractor=content_extractor,
        request_repo=request_repo,
    )
