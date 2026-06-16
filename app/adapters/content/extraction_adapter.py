"""Concrete ``ExtractionPort`` adapter (ADR-0015).

Fuses the URL-pattern platform router + multi-provider scraper chain behind the
single application ``ExtractionPort`` the graph ``extract`` node calls. It does
NOT re-implement extraction: it delegates to
``ContentExtractor.extract_content_pure`` -- the established Telegram-free pure
path that already runs the ``PlatformExtractionRouter`` (pure mode) first,
falls back to ``ContentScraperChain.scrape_markdown`` inside the shared
semaphore, applies the low-value guard, and persists extraction failures via
``persist_request_failure`` (``REASON_FIRECRAWL_LOW_VALUE`` /
``REASON_FIRECRAWL_ERROR``). The scraper chain stays a cohesive algorithm inside
``ContentScraperChain`` -- rungs are NOT exploded into graph nodes (ADR-0015).

This is the ONE adapter-layer module the extract node reaches (via DI); the node
itself imports only the application port + DTOs (``application-no-outward``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.application.ports.extraction import ExtractionRequest, ExtractionResult
from app.core.lang import detect_language
from app.core.logging_utils import get_logger
from app.core.url_utils import normalize_url, url_hash_sha256

if TYPE_CHECKING:
    from app.adapters.content.content_extractor import ContentExtractor
    from app.application.ports.requests import RequestRepositoryPort

logger = get_logger(__name__)


class ContentExtractionAdapter:
    """``ExtractionPort`` implementation backed by ``ContentExtractor``."""

    def __init__(
        self,
        *,
        content_extractor: ContentExtractor,
        request_repo: RequestRepositoryPort,
    ) -> None:
        self._content_extractor = content_extractor
        self._request_repo = request_repo

    async def extract(self, request: ExtractionRequest) -> ExtractionResult:
        """Extract content for ``request`` via the pure router+chain path.

        Re-raises the ``ValueError`` that ``extract_content_pure`` raises on a
        (already-persisted) extraction failure so the extract node routes it to
        the single terminal-failure path (ADR-0011).
        """
        content_text, content_source, metadata = await self._content_extractor.extract_content_pure(
            request.url,
            correlation_id=request.correlation_id,
            request_id=request.request_id,
        )

        detected_lang = str(metadata.get("detected_lang") or detect_language(content_text or ""))

        # Persist detected language against the request row -- parity with the
        # interactive path (extract_and_process_content) which the pure path omits.
        if request.request_id is not None:
            try:
                await self._request_repo.async_update_request_lang_detected(
                    request.request_id, detected_lang
                )
            except Exception:  # best-effort: lang persistence must not fail extraction
                logger.warning(
                    "extraction_adapter_lang_persist_failed",
                    extra={"cid": request.correlation_id, "request_id": request.request_id},
                    exc_info=True,
                )

        dedupe_hash = url_hash_sha256(normalize_url(request.url))

        return ExtractionResult(
            request_id=request.request_id,
            content_text=content_text,
            content_source=content_source,
            detected_lang=detected_lang,
            dedupe_hash=dedupe_hash,
            title=_extract_title(metadata),
            # Pure-path parity: the summarize path routes with has_images=False, so
            # the graph carries no image handles (vision routing is out of scope here).
            images=[],
            metadata={
                "extraction_method": metadata.get("extraction_method"),
                "http_status": metadata.get("http_status"),
                "content_length": metadata.get("content_length"),
                "source_format": metadata.get("source_format"),
            },
        )


def _extract_title(metadata: dict[str, Any]) -> str | None:
    """Pull a title out of the pure-extraction metadata (firecrawl/NSD), or None."""
    firecrawl_meta = metadata.get("firecrawl_metadata")
    if isinstance(firecrawl_meta, dict):
        title = firecrawl_meta.get("title") or firecrawl_meta.get("og:title")
        if title:
            return str(title)
    nsd = metadata.get("normalized_source_document")
    if isinstance(nsd, dict):
        title = nsd.get("title")
        if title:
            return str(title)
    return None
