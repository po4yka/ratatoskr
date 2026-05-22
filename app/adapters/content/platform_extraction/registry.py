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
class PlatformExtractorContext:
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
class PlatformExtractorContribution:
    """Descriptor for one lazily-created platform extractor route."""

    name: str
    predicate: Callable[[str], bool]
    factory: Callable[[PlatformExtractorContext], PlatformExtractor]


def build_platform_extraction_router(
    contributions: Sequence[PlatformExtractorContribution],
    context: PlatformExtractorContext,
) -> PlatformExtractionRouter:
    """Build a router from platform extractor contributions in declared order."""
    router = PlatformExtractionRouter()

    def bind_factory(
        contribution: PlatformExtractorContribution,
    ) -> Callable[[], PlatformExtractor]:
        return lambda: contribution.factory(context)

    for contribution in contributions:
        router.register(
            predicate=contribution.predicate,
            factory=bind_factory(contribution),
        )
    return router
