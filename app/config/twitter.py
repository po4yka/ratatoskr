"""Twitter/X content extraction configuration."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationInfo, field_validator, model_validator

_SCRAPER_PROFILES = {"fast", "balanced", "robust", "inherit"}
_FORCE_TIERS = {"auto", "firecrawl", "playwright"}
_DEFAULT_X_OAUTH_SCOPES = ("tweet.read", "users.read", "offline.access")


class TwitterConfig(BaseModel):
    """Twitter/X content extraction configuration."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=True,
        validation_alias="TWITTER_ENABLED",
        description="Enable Twitter/X URL detection and extraction",
    )

    playwright_enabled: bool = Field(
        default=False,
        validation_alias="TWITTER_PLAYWRIGHT_ENABLED",
        description="Enable Playwright-based extraction (requires playwright + chromium)",
    )

    force_tier: str = Field(
        default="auto",
        validation_alias="TWITTER_FORCE_TIER",
        description="Force extraction tier: auto, firecrawl, playwright",
    )

    scraper_profile: str = Field(
        default="inherit",
        validation_alias="TWITTER_SCRAPER_PROFILE",
        description="Twitter scraper profile override (inherit, fast, balanced, robust)",
    )

    max_concurrent_browsers: int = Field(
        default=2,
        validation_alias="TWITTER_MAX_CONCURRENT_BROWSERS",
        description="Maximum concurrent Twitter Playwright browser sessions",
    )

    cookies_path: str = Field(
        default="/data/twitter_cookies.txt",
        validation_alias="TWITTER_COOKIES_PATH",
        description="Path to Netscape-format cookies.txt for authenticated extraction",
    )

    headless: bool = Field(
        default=True,
        validation_alias="TWITTER_HEADLESS",
        description="Run Playwright browser in headless mode",
    )

    page_timeout_ms: int = Field(
        default=15000,
        validation_alias="TWITTER_PAGE_TIMEOUT_MS",
        description="Page load timeout in milliseconds for Playwright",
    )

    prefer_firecrawl: bool = Field(
        default=True,
        validation_alias="TWITTER_PREFER_FIRECRAWL",
        description="Try Firecrawl first before falling back to Playwright",
    )

    article_redirect_resolution_enabled: bool = Field(
        default=True,
        validation_alias="TWITTER_ARTICLE_REDIRECT_RESOLUTION_ENABLED",
        description="Resolve redirects/canonical hints for X Article links before extraction",
    )

    article_resolution_timeout_sec: float = Field(
        default=5.0,
        validation_alias="TWITTER_ARTICLE_RESOLUTION_TIMEOUT_SEC",
        description="Timeout in seconds for resolving redirected X Article links",
    )

    article_live_smoke_enabled: bool = Field(
        default=False,
        validation_alias="TWITTER_ARTICLE_LIVE_SMOKE_ENABLED",
        description="Enable optional live smoke checks for X Article extraction",
    )
    x_oauth_client_id: str | None = Field(
        default=None,
        validation_alias="X_OAUTH_CLIENT_ID",
        description="X OAuth 2.0 client ID for Authorization Code with PKCE",
    )
    x_oauth_client_secret: SecretStr | None = Field(
        default=None,
        validation_alias="X_OAUTH_CLIENT_SECRET",
        description="Optional X OAuth 2.0 client secret for confidential clients",
    )
    x_oauth_redirect_uri: str | None = Field(
        default=None,
        validation_alias="X_OAUTH_REDIRECT_URI",
        description="Configured X OAuth callback URI",
    )
    x_oauth_scopes: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_X_OAUTH_SCOPES),
        validation_alias="X_OAUTH_SCOPES",
        description="Read-only X OAuth scopes",
    )
    x_api_base_url: str = Field(
        default="https://api.x.com/2",
        validation_alias="X_API_BASE_URL",
        description="X API v2 base URL",
    )

    @field_validator("force_tier", mode="before")
    @classmethod
    def _validate_force_tier(cls, value: Any) -> str:
        tier = str(value or "auto").strip().lower()
        if tier not in _FORCE_TIERS:
            msg = "TWITTER_FORCE_TIER must be one of: auto, firecrawl, playwright"
            raise ValueError(msg)
        return tier

    @field_validator("scraper_profile", mode="before")
    @classmethod
    def _validate_scraper_profile(cls, value: Any) -> str:
        profile = str(value or "inherit").strip().lower()
        if profile not in _SCRAPER_PROFILES:
            msg = "TWITTER_SCRAPER_PROFILE must be one of: inherit, fast, balanced, robust"
            raise ValueError(msg)
        return profile

    @field_validator("max_concurrent_browsers", mode="before")
    @classmethod
    def _validate_max_concurrent_browsers(cls, value: Any) -> int:
        raw = 2 if value in (None, "") else value
        try:
            parsed = int(str(raw))
        except ValueError as exc:
            msg = "TWITTER_MAX_CONCURRENT_BROWSERS must be a valid integer"
            raise ValueError(msg) from exc
        if parsed < 1 or parsed > 20:
            msg = "TWITTER_MAX_CONCURRENT_BROWSERS must be between 1 and 20"
            raise ValueError(msg)
        return parsed

    @field_validator("page_timeout_ms", mode="before")
    @classmethod
    def _parse_timeout(cls, value: Any, info: ValidationInfo) -> int:
        if value in (None, ""):
            return 15000
        try:
            timeout = int(str(value))
        except ValueError as exc:
            msg = "page_timeout_ms must be a valid integer"
            raise ValueError(msg) from exc
        if timeout < 500 or timeout > 120_000:
            msg = "page_timeout_ms must be between 500 and 120000"
            raise ValueError(msg)
        return timeout

    @field_validator("article_resolution_timeout_sec", mode="before")
    @classmethod
    def _parse_article_resolution_timeout(cls, value: Any, info: ValidationInfo) -> float:
        if value in (None, ""):
            return 5.0
        try:
            timeout = float(str(value))
        except ValueError as exc:
            msg = "article_resolution_timeout_sec must be a valid number"
            raise ValueError(msg) from exc
        if timeout <= 0:
            msg = "article_resolution_timeout_sec must be greater than 0"
            raise ValueError(msg)
        if timeout > 120:
            msg = "article_resolution_timeout_sec must be 120 seconds or less"
            raise ValueError(msg)
        return timeout

    @field_validator("x_oauth_scopes", mode="before")
    @classmethod
    def _parse_x_oauth_scopes(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return list(_DEFAULT_X_OAUTH_SCOPES)
        if isinstance(value, str):
            raw_scopes = value.replace(",", " ").split()
        elif isinstance(value, list):
            raw_scopes = [str(item) for item in value]
        else:
            msg = "X_OAUTH_SCOPES must be a comma- or space-separated scope list"
            raise ValueError(msg)

        scopes: list[str] = []
        for raw_scope in raw_scopes:
            scope = raw_scope.strip()
            if scope and scope not in scopes:
                scopes.append(scope)
        return scopes or list(_DEFAULT_X_OAUTH_SCOPES)

    @field_validator("x_oauth_scopes")
    @classmethod
    def _validate_x_oauth_scopes(cls, value: list[str]) -> list[str]:
        write_scopes = sorted(scope for scope in value if scope.endswith(".write"))
        if write_scopes:
            msg = f"X_OAUTH_SCOPES must not include write scopes: {', '.join(write_scopes)}"
            raise ValueError(msg)
        return value

    @field_validator("x_api_base_url", mode="before")
    @classmethod
    def _normalize_x_api_base_url(cls, value: Any) -> str:
        base_url = str(value or "https://api.x.com/2").strip().rstrip("/")
        if not base_url:
            msg = "X_API_BASE_URL must not be empty"
            raise ValueError(msg)
        return base_url

    @model_validator(mode="after")
    def _validate_extraction_tiers(self) -> TwitterConfig:
        if self.force_tier == "firecrawl" and not self.prefer_firecrawl:
            msg = "TWITTER_FORCE_TIER=firecrawl requires TWITTER_PREFER_FIRECRAWL=true"
            raise ValueError(msg)

        if self.force_tier == "playwright" and not self.playwright_enabled:
            msg = "TWITTER_FORCE_TIER=playwright requires TWITTER_PLAYWRIGHT_ENABLED=true"
            raise ValueError(msg)

        if not self.prefer_firecrawl and not self.playwright_enabled:
            msg = (
                "At least one Twitter extraction tier must be enabled "
                "(TWITTER_PREFER_FIRECRAWL or TWITTER_PLAYWRIGHT_ENABLED)."
            )
            raise ValueError(msg)

        return self
