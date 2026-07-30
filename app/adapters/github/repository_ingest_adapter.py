"""Adapter: expose the GitHub platform extractor as a RepositoryIngestPort.

The extractor speaks the content-extraction envelope
(``PlatformExtractionRequest`` / ``PlatformExtractionResult``) because it is one
rung of the URL pipeline. A use case that only wants "ingest this repo and tell me
its id" should not have to build that envelope, so the translation lives here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.adapters.github.exceptions import GitHubNotFoundError
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from app.adapters.github.platform_extractor import GitHubPlatformExtractor

logger = get_logger(__name__)


class GitHubRepositoryIngestAdapter:
    """Ingest a GitHub repository by URL and return its local row id."""

    def __init__(self, extractor: GitHubPlatformExtractor) -> None:
        self._extractor = extractor

    async def ingest(self, *, url: str, user_id: int, correlation_id: str) -> int:
        from app.adapters.content.platform_extraction.models import PlatformExtractionRequest

        result = await self._extractor.extract(
            PlatformExtractionRequest(
                message=None,
                url_text=url,
                normalized_url=url,
                correlation_id=correlation_id,
                user_id=user_id,
                mode="pure",
            )
        )

        metadata = result.metadata or {}
        repository_id = int(metadata.get("repository_id") or 0)
        if repository_id <= 0:
            # The extractor upserts before composing its result, so a missing id
            # means the contract changed rather than that the repo is absent.
            raise GitHubNotFoundError(f"GitHub ingest returned no repository id for {url}")
        return repository_id
