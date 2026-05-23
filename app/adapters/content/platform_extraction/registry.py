"""Registry helpers for additive platform extractor wiring."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.adapters.content.platform_extraction.router import PlatformExtractionRouter

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from app.adapters.content.platform_extraction.lifecycle import PlatformRequestLifecycle
    from app.adapters.content.platform_extraction.protocol import PlatformExtractor
    from app.adapters.content.scraper.protocol import ContentScraperProtocol
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )
    from app.adapters.llm.protocol import LLMClientProtocol
    from app.config import AppConfig
    from app.db.session import Database
    from app.infrastructure.persistence.message_persistence import MessagePersistence


@dataclass(frozen=True, slots=True)
class PlatformExtractorBuildContext:
    """Runtime dependencies available to platform extractor factories."""

    cfg: AppConfig
    db: Database
    scraper: ContentScraperProtocol
    response_formatter: ResponseFormatter
    audit_func: Callable[[str, str, dict[str, Any]], None]
    sem: Callable[[], Any]
    message_persistence: MessagePersistence
    lifecycle: PlatformRequestLifecycle
    quality_llm_client: LLMClientProtocol | None
    schedule_crawl_persistence: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class PlatformExtractorDescriptor:
    """Descriptor for one lazily-created platform extractor route."""

    name: str
    predicate: Callable[[str], bool]
    factory: Callable[[PlatformExtractorBuildContext], PlatformExtractor]


PlatformExtractorContext = PlatformExtractorBuildContext
PlatformExtractorContribution = PlatformExtractorDescriptor


def build_platform_extraction_router(
    descriptors: Sequence[PlatformExtractorDescriptor],
    context: PlatformExtractorBuildContext,
) -> PlatformExtractionRouter:
    """Build a router from platform extractor descriptors in declared order."""
    router = PlatformExtractionRouter()

    def bind_factory(
        descriptor: PlatformExtractorDescriptor,
    ) -> Callable[[], PlatformExtractor]:
        return lambda: descriptor.factory(context)

    for descriptor in descriptors:
        router.register(
            predicate=descriptor.predicate,
            factory=bind_factory(descriptor),
        )
    return router
