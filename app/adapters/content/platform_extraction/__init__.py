"""Shared platform extraction framework for platform-specific URL handlers."""

from app.adapters.content.platform_extraction.lifecycle import PlatformRequestLifecycle
from app.adapters.content.platform_extraction.models import (
    PlatformExtractionMode,
    PlatformExtractionRequest,
    PlatformExtractionResult,
)
from app.adapters.content.platform_extraction.protocol import PlatformExtractor
from app.adapters.content.platform_extraction.registry import (
    PlatformExtractorBuildContext,
    PlatformExtractorContext,
    PlatformExtractorContribution,
    PlatformExtractorDescriptor,
    build_platform_extraction_router,
)
from app.adapters.content.platform_extraction.router import PlatformExtractionRouter

__all__ = [
    "PlatformExtractionMode",
    "PlatformExtractionRequest",
    "PlatformExtractionResult",
    "PlatformExtractionRouter",
    "PlatformExtractor",
    "PlatformExtractorBuildContext",
    "PlatformExtractorContext",
    "PlatformExtractorContribution",
    "PlatformExtractorDescriptor",
    "PlatformRequestLifecycle",
    "build_platform_extraction_router",
]
