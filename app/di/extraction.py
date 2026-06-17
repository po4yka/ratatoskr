"""Composition root for the content-extraction port (ADR-0010/0015).

Wires the concrete ``ContentExtractionAdapter`` (which fuses the platform router
+ scraper chain via ``ContentExtractor``) into the application ``ExtractionPort``
the summarize graph's ``extract`` node depends on. This is the only place the
graph's extraction seam touches the adapter layer; nodes receive the port via
``SummarizeDeps`` (``app.di.graphs.build_summarize_deps``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.adapters.content.content_extractor import ContentExtractor
    from app.application.ports.extraction import ExtractionPort
    from app.application.ports.requests import RequestRepositoryPort


def build_extraction_port(
    *,
    content_extractor: ContentExtractor,
    request_repo: RequestRepositoryPort,
) -> ExtractionPort:
    """Construct the ``ExtractionPort`` from an already-built ``ContentExtractor``.

    ``app.di`` is the sanctioned composition root for this concrete adapter
    (ADR-0015): the import-linter contract "content extraction adapter is reached
    only via the ExtractionPort / di" forbids ``app.api`` / ``app.tasks`` /
    ``app.application`` from importing it, while explicitly allowing the ``app.di``
    wiring seam (this edge is whitelisted via ``ignore_imports``).
    """
    from app.adapters.content.extraction_adapter import ContentExtractionAdapter

    return ContentExtractionAdapter(
        content_extractor=content_extractor,
        request_repo=request_repo,
    )
