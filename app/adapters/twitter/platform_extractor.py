"""Platform extractor for Twitter/X URLs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.adapters.content.platform_extraction.protocol import PlatformExtractor
from app.adapters.social.x import XOAuthClient, XOAuthConfig
from app.adapters.twitter.extraction_coordinator import TwitterExtractionCoordinator
from app.adapters.twitter.firecrawl_extractor import TwitterFirecrawlExtractor
from app.adapters.twitter.playwright_extractor import TwitterPlaywrightExtractor
from app.adapters.twitter.tier_policy import TwitterTierPolicy
from app.application.services.social_token_service import SocialAccessTokenResolver
from app.core.urls.twitter import is_twitter_url
from app.infrastructure.persistence.repositories.social_connection_repository import (
    SocialConnectionRepositoryAdapter,
)

if TYPE_CHECKING:
    from app.adapters.content.platform_extraction.lifecycle import PlatformRequestLifecycle
    from app.adapters.content.platform_extraction.models import (
        PlatformExtractionRequest,
        PlatformExtractionResult,
    )
    from app.adapters.content.scraper.protocol import ContentScraperProtocol
    from app.adapters.external.formatting.protocols import (
        ResponseFormatterFacade as ResponseFormatter,
    )


class TwitterPlatformExtractor(PlatformExtractor):
    """Platform extractor for Twitter/X content."""

    def __init__(
        self,
        *,
        cfg: Any,
        db: Any,
        firecrawl: ContentScraperProtocol,
        response_formatter: ResponseFormatter,
        message_persistence: Any,
        firecrawl_sem: Any,
        schedule_crawl_persistence: Any,
        lifecycle: PlatformRequestLifecycle,
    ) -> None:
        request_repo = message_persistence.request_repo
        tier_policy = TwitterTierPolicy(cfg=cfg)
        twitter_cfg = cfg.twitter
        from app.adapters.twitter.api_extractor import XApiPostExtractor

        x_client_secret = getattr(twitter_cfg, "x_oauth_client_secret", None)
        social_repository = SocialConnectionRepositoryAdapter(db)
        x_client = XOAuthClient(
            XOAuthConfig(
                client_id=getattr(twitter_cfg, "x_oauth_client_id", None),
                client_secret=x_client_secret.get_secret_value()
                if x_client_secret is not None
                else None,
                redirect_uri=getattr(twitter_cfg, "x_oauth_redirect_uri", None),
                scopes=getattr(twitter_cfg, "x_oauth_scopes", None),
                api_base_url=getattr(
                    twitter_cfg,
                    "x_api_base_url",
                    "https://api.x.com/2",
                ),
            )
        )
        x_api_extractor = XApiPostExtractor(
            repository=social_repository,
            x_client=x_client,
            token_resolver=SocialAccessTokenResolver(
                repository=social_repository,
                oauth_clients={"x": x_client},
            ),
        )
        firecrawl_extractor = TwitterFirecrawlExtractor(
            firecrawl=firecrawl,
            firecrawl_sem=firecrawl_sem,
            schedule_crawl_persistence=schedule_crawl_persistence,
            request_repo=request_repo,
        )
        playwright_extractor = TwitterPlaywrightExtractor(
            cfg=cfg,
            request_repo=request_repo,
        )
        self._coordinator = TwitterExtractionCoordinator(
            cfg=cfg,
            response_formatter=response_formatter,
            request_repo=request_repo,
            lifecycle=lifecycle,
            tier_policy=tier_policy,
            x_api_extractor=x_api_extractor,
            firecrawl_extractor=firecrawl_extractor,
            playwright_extractor=playwright_extractor,
        )

    def supports(self, normalized_url: str) -> bool:
        return is_twitter_url(normalized_url)

    async def extract(self, request: PlatformExtractionRequest) -> PlatformExtractionResult:
        return await self._coordinator.extract(request)
