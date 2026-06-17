"""Platform extractor for GitHub repository URLs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.adapters.github.exceptions import GitHubIntegrationRequiredError
from app.adapters.github.github_api_client import GitHubAPIClient
from app.adapters.github.url_patterns import is_github_repo_url, parse_github_repo_url
from app.core.logging_utils import get_logger
from app.db.models.repository import (
    GitHubIntegrationStatus,
    Repository,
    RepoSource,
    UserGitHubIntegration,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from app.adapters.content.platform_extraction.models import (
        PlatformExtractionRequest,
        PlatformExtractionResult,
    )
    from app.application.use_cases.analyze_repository import AnalyzeRepositoryUseCase
    from app.config.github import GitHubConfig
    from app.db.session import Database

logger = get_logger(__name__)


def truncate_readme(content: str | None, max_bytes: int) -> str | None:
    """Truncate to max_bytes when UTF-8 encoded; cut at character boundary."""
    if content is None:
        return None
    encoded = content.encode("utf-8")
    if len(encoded) <= max_bytes:
        return content
    # Walk back to a valid char boundary
    truncated = encoded[:max_bytes]
    while truncated and (truncated[-1] & 0xC0) == 0x80:
        truncated = truncated[:-1]
    return truncated.decode("utf-8", errors="ignore")


def _default_client_factory(token: str) -> GitHubAPIClient:
    return GitHubAPIClient(token)


class GitHubPlatformExtractor:
    """PlatformExtractor for GitHub repo URLs.

    Precondition: an active UserGitHubIntegration must exist for the user.
    Raises GitHubIntegrationRequiredError otherwise.
    """

    def __init__(
        self,
        db: Database,
        github_config: GitHubConfig,
        analyze_use_case: AnalyzeRepositoryUseCase,
        client_factory: Callable[[str], GitHubAPIClient] | None = None,
    ) -> None:
        self._db = db
        self._github_config = github_config
        self._analyze_use_case = analyze_use_case
        self._client_factory = client_factory or _default_client_factory

    def supports(self, normalized_url: str) -> bool:
        return is_github_repo_url(normalized_url)

    async def extract(self, request: PlatformExtractionRequest) -> PlatformExtractionResult:
        from app.adapters.content.platform_extraction.models import PlatformExtractionResult

        # 1. Parse owner, name from URL
        parsed = parse_github_repo_url(request.normalized_url)
        if parsed is None:
            raise ValueError(f"Cannot parse GitHub repo URL: {request.normalized_url!r}")
        owner, name = parsed

        # 2. Resolve user_id from request
        user_id = request.user_id
        if user_id is None:
            raise ValueError("PlatformExtractionRequest.user_id is required for GitHub extraction")

        # 3. Load active UserGitHubIntegration; raise if missing/inactive
        integration = await self._load_active_integration(user_id)

        # 4. Decrypt token
        from app.security.token_crypto import decrypt_token

        token = decrypt_token(integration.encrypted_token)

        # 5. Construct GitHubAPIClient via factory
        client = self._client_factory(token)

        # 6. Fetch repo, then a conditional README — reuse a stored ETag so an
        #    unchanged README costs a free 304 instead of a full download.
        async with client:
            repo_dto = await client.get_repo(owner, name)
            cached_etag, cached_excerpt = await self._load_cached_readme(
                user_id, repo_dto_id=repo_dto.id
            )
            readme = await client.get_readme(owner, name, etag=cached_etag)
            languages = await client.get_languages(owner, name)

        if readme.not_modified:
            # 304: keep the README excerpt and ETag we already stored so the
            # content hash stays stable and analysis is not re-run needlessly.
            readme_excerpt = cached_excerpt
            readme_etag = cached_etag
        else:
            readme_excerpt = truncate_readme(readme.content, self._github_config.readme_max_bytes)
            readme_etag = readme.etag

        logger.info(
            "github_extractor_fetched",
            extra={
                "owner": owner,
                "name": name,
                "user_id": user_id,
                "has_readme": readme_excerpt is not None,
                "cid": request.correlation_id,
            },
        )

        # 7. UPSERT Repository row
        repository_id = await self._upsert_repository(
            user_id=user_id,
            owner=owner,
            name=name,
            repo_dto=repo_dto,
            languages=languages,
            readme_excerpt=readme_excerpt,
            readme_etag=readme_etag,
        )

        # 8. Enqueue analysis (await directly for manual ingestion path)
        await self._analyze_use_case.analyze(
            repository_id,
            force=False,
            correlation_id=request.correlation_id or "",
            chosen_lang="en",
        )

        # 9. Compose PlatformExtractionResult
        content_text = (repo_dto.description or "") + "\n\n# README\n\n" + (readme_excerpt or "")

        metadata = {
            "platform": "github",
            "github_id": repo_dto.id,
            "owner": owner,
            "name": name,
            "full_name": repo_dto.full_name,
            "stars": repo_dto.stargazers_count,
            "primary_language": repo_dto.language,
            "topics": list(repo_dto.topics),
            "default_branch": repo_dto.default_branch,
            "license": repo_dto.license.spdx_id if repo_dto.license else None,
        }

        return PlatformExtractionResult(
            platform="github",
            request_id=request.request_id_override,
            content_text=content_text,
            content_source="github_api",
            detected_lang="en",
            title=repo_dto.full_name,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _load_active_integration(self, user_id: int) -> UserGitHubIntegration:
        async with self._db.session() as session:
            stmt = select(UserGitHubIntegration).where(UserGitHubIntegration.user_id == user_id)
            result = await session.execute(stmt)
            integration = result.scalar_one_or_none()

        if integration is None or integration.status != GitHubIntegrationStatus.ACTIVE:
            raise GitHubIntegrationRequiredError(
                f"No active GitHub integration for user_id={user_id}. "
                "Connect GitHub first via /github connect."
            )
        return integration

    async def _load_cached_readme(
        self, user_id: int, *, repo_dto_id: int
    ) -> tuple[str | None, str | None]:
        """Return (readme_etag, readme_excerpt) for an existing row, or (None, None).

        Used only to make the README fetch conditional; a missing row means a
        first-time ingest and an unconditional fetch.
        """
        async with self._db.session() as session:
            result = await session.execute(
                select(Repository.readme_etag, Repository.readme_excerpt).where(
                    Repository.github_id == repo_dto_id,
                    Repository.user_id == user_id,
                )
            )
            row = result.first()
        if row is None:
            return None, None
        return row[0], row[1]

    async def _upsert_repository(
        self,
        *,
        user_id: int,
        owner: str,
        name: str,
        repo_dto: object,
        languages: dict[str, int],
        readme_excerpt: str | None,
        readme_etag: str | None,
    ) -> int:
        from app.adapters.github.types import RepositoryDTO

        assert isinstance(repo_dto, RepositoryDTO)

        now = datetime.now(UTC)
        license_spdx = repo_dto.license.spdx_id if repo_dto.license else None

        insert_values = {
            "github_id": repo_dto.id,
            "owner": owner,
            "name": name,
            "full_name": repo_dto.full_name,
            "url": repo_dto.html_url,
            "homepage_url": repo_dto.homepage,
            "description": repo_dto.description,
            "primary_language": repo_dto.language,
            "languages_json": languages,
            "topics_json": list(repo_dto.topics),
            "stars": repo_dto.stargazers_count,
            "forks": repo_dto.forks_count,
            "watchers": repo_dto.watchers_count,
            "default_branch": repo_dto.default_branch,
            "license_spdx": license_spdx,
            "is_archived": repo_dto.archived,
            "is_fork": repo_dto.fork,
            "is_template": repo_dto.is_template,
            "pushed_at": repo_dto.pushed_at,
            "created_at_github": repo_dto.created_at,
            "readme_excerpt": readme_excerpt,
            "readme_etag": readme_etag,
            "source": RepoSource.MANUAL,
            "is_starred": False,
            "user_id": user_id,
            "last_synced_at": now,
        }

        update_set = {
            "description": repo_dto.description,
            "primary_language": repo_dto.language,
            "languages_json": languages,
            "topics_json": list(repo_dto.topics),
            "stars": repo_dto.stargazers_count,
            "forks": repo_dto.forks_count,
            "watchers": repo_dto.watchers_count,
            "default_branch": repo_dto.default_branch,
            "license_spdx": license_spdx,
            "is_archived": repo_dto.archived,
            "is_fork": repo_dto.fork,
            "is_template": repo_dto.is_template,
            "pushed_at": repo_dto.pushed_at,
            "homepage_url": repo_dto.homepage,
            "readme_excerpt": readme_excerpt,
            "readme_etag": readme_etag,
            "last_synced_at": now,
        }

        stmt = (
            insert(Repository)
            .values(**insert_values)
            .on_conflict_do_update(
                constraint="uq_repositories_user_github",
                set_=update_set,
            )
            .returning(Repository.id)
        )

        async with self._db.transaction() as session:
            repository_id = int(await session.scalar(stmt) or 0)

        logger.info(
            "github_repository_upserted",
            extra={
                "repository_id": repository_id,
                "full_name": repo_dto.full_name,
                "user_id": user_id,
            },
        )
        return repository_id
