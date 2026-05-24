"""Scraper multi-provider configuration."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from ._secret_marker import SECRET_MARKER

SCRAPER_PROVIDER_TOKENS = {
    "scrapling",
    "defuddle",
    "firecrawl",
    "cloakbrowser",
    "playwright",
    "crawlee",
    "direct_html",
    "direct_pdf",
    "crawl4ai",
    "scrapegraph_ai",
}

SCRAPER_PROFILES = {"fast", "balanced", "robust"}
DEFAULT_SCRAPER_PROVIDER_ORDER = [
    "scrapling",
    "direct_pdf",
    "crawl4ai",
    "firecrawl",
    "defuddle",
    "cloakbrowser",
    "playwright",
    "crawlee",
    "direct_html",
    "scrapegraph_ai",
]

_PROFILE_TIMEOUT_MULTIPLIERS = {
    "fast": 0.75,
    "balanced": 1.0,
    "robust": 1.35,
}


def profile_timeout_multiplier(profile: str) -> float:
    normalized = profile.strip().lower()
    if normalized not in SCRAPER_PROFILES:
        normalized = "balanced"
    return _PROFILE_TIMEOUT_MULTIPLIERS[normalized]


def profile_retry_budget(base_retries: int, profile: str) -> int:
    normalized = profile.strip().lower()
    retries = max(0, int(base_retries))
    if normalized == "fast":
        return min(retries, 1)
    if normalized == "robust":
        return min(retries + 1, 5)
    return retries


class ScraperConfig(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    enabled: bool = Field(
        default=True,
        validation_alias="SCRAPER_ENABLED",
        description="Global switch for article scraping providers",
    )
    profile: str = Field(
        default="balanced",
        validation_alias="SCRAPER_PROFILE",
        description="Scraper tuning profile: fast, balanced, robust",
    )
    browser_enabled: bool = Field(
        default=True,
        validation_alias="SCRAPER_BROWSER_ENABLED",
        description="Master switch for browser-based providers (playwright/crawlee)",
    )
    force_provider: str | None = Field(
        default=None,
        validation_alias="SCRAPER_FORCE_PROVIDER",
        description="If set, only this provider token is used",
    )
    js_heavy_hosts: tuple[str, ...] = Field(
        default_factory=tuple,
        validation_alias="SCRAPER_JS_HEAVY_HOSTS",
        description="CSV or list of hosts treated as JavaScript-heavy",
    )
    min_content_length: int = Field(
        default=400,
        validation_alias="SCRAPER_MIN_CONTENT_LENGTH",
        description="Minimum extracted text length required to accept a scrape",
    )
    allow_private_network_urls: bool = Field(
        default=False,
        validation_alias="SCRAPER_ALLOW_PRIVATE_NETWORK_URLS",
        description=(
            "Local-development override for fetching localhost and RFC1918 target URLs; "
            "metadata, link-local, reserved, and non-http(s) URLs remain blocked"
        ),
    )

    provider_order: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SCRAPER_PROVIDER_ORDER),
        validation_alias="SCRAPER_PROVIDER_ORDER",
        description="Ordered list of scraping providers to try",
    )

    scrapling_enabled: bool = Field(
        default=True,
        validation_alias="SCRAPER_SCRAPLING_ENABLED",
    )
    scrapling_timeout_sec: int = Field(
        default=30,
        validation_alias="SCRAPER_SCRAPLING_TIMEOUT_SEC",
    )
    scrapling_stealth_fallback: bool = Field(
        default=True,
        validation_alias="SCRAPER_SCRAPLING_STEALTH_FALLBACK",
    )

    defuddle_enabled: bool = Field(
        default=True,
        validation_alias="SCRAPER_DEFUDDLE_ENABLED",
    )
    defuddle_timeout_sec: int = Field(
        default=20,
        validation_alias="SCRAPER_DEFUDDLE_TIMEOUT_SEC",
    )
    defuddle_api_base_url: str = Field(
        default="http://defuddle-api:3003",
        validation_alias="SCRAPER_DEFUDDLE_API_BASE_URL",
    )
    defuddle_token: str = Field(
        default="",
        validation_alias="SCRAPER_DEFUDDLE_TOKEN",
        description="Bearer token for self-hosted Defuddle sidecar; empty disables auth",
        json_schema_extra=SECRET_MARKER,
    )

    firecrawl_self_hosted_enabled: bool = Field(
        default=False,
        validation_alias="FIRECRAWL_SELF_HOSTED_ENABLED",
    )
    firecrawl_self_hosted_url: str = Field(
        default="http://firecrawl-api:3002",
        validation_alias="FIRECRAWL_SELF_HOSTED_URL",
    )
    firecrawl_self_hosted_api_key: str = Field(
        default="fc-ratatoskr-local",
        validation_alias="FIRECRAWL_SELF_HOSTED_API_KEY",
        json_schema_extra=SECRET_MARKER,
    )

    firecrawl_timeout_sec: int = Field(
        default=90,
        validation_alias="SCRAPER_FIRECRAWL_TIMEOUT_SEC",
    )
    firecrawl_wait_for_ms: int = Field(
        default=3000,
        validation_alias="SCRAPER_FIRECRAWL_WAIT_FOR_MS",
    )
    firecrawl_max_retries: int = Field(
        default=3,
        validation_alias="SCRAPER_FIRECRAWL_MAX_RETRIES",
    )
    firecrawl_max_connections: int = Field(
        default=10,
        validation_alias="SCRAPER_FIRECRAWL_MAX_CONNECTIONS",
    )
    firecrawl_max_keepalive_connections: int = Field(
        default=5,
        validation_alias="SCRAPER_FIRECRAWL_MAX_KEEPALIVE_CONNECTIONS",
    )
    firecrawl_keepalive_expiry: float = Field(
        default=30.0,
        validation_alias="SCRAPER_FIRECRAWL_KEEPALIVE_EXPIRY",
    )
    firecrawl_max_response_size_mb: int = Field(
        default=50,
        validation_alias="SCRAPER_FIRECRAWL_MAX_RESPONSE_SIZE_MB",
    )

    cloakbrowser_enabled: bool = Field(
        default=True,
        validation_alias="SCRAPER_CLOAKBROWSER_ENABLED",
        description=(
            "Enable the CloakBrowser CDP-sidecar provider. Active only when the "
            "`with-scrapers` Docker profile brings up the cloakbrowser service; "
            "otherwise the provider build fails and the chain falls through to "
            "the in-process playwright rung."
        ),
    )
    cloakbrowser_url: str = Field(
        default="http://cloakbrowser:9222",
        validation_alias="SCRAPER_CLOAKBROWSER_URL",
        description=(
            "HTTP endpoint of the CloakBrowser cloakserve CDP server. Playwright "
            "resolves the WebSocket debugger URL from /json/version on this host."
        ),
    )
    cloakbrowser_timeout_sec: int = Field(
        default=60,
        validation_alias="SCRAPER_CLOAKBROWSER_TIMEOUT_SEC",
    )
    cloakbrowser_humanize: bool = Field(
        default=True,
        validation_alias="SCRAPER_CLOAKBROWSER_HUMANIZE",
        description=(
            "Apply post-connect humanize layer (bezier mouse/scroll pacing) so "
            "behavioral signals look human to Cloudflare/Turnstile scoring."
        ),
    )
    cloakbrowser_proxy: str = Field(
        default="",
        validation_alias="SCRAPER_CLOAKBROWSER_PROXY",
        description=(
            "Optional proxy URL (HTTP/SOCKS5) forwarded to cloakserve per "
            "request via the ?proxy= query param. Empty disables."
        ),
    )

    playwright_enabled: bool = Field(
        default=True,
        validation_alias="SCRAPER_PLAYWRIGHT_ENABLED",
    )
    playwright_headless: bool = Field(
        default=True,
        validation_alias="SCRAPER_PLAYWRIGHT_HEADLESS",
    )
    playwright_timeout_sec: int = Field(
        default=30,
        validation_alias="SCRAPER_PLAYWRIGHT_TIMEOUT_SEC",
    )

    crawlee_enabled: bool = Field(
        default=True,
        validation_alias="SCRAPER_CRAWLEE_ENABLED",
    )
    crawlee_timeout_sec: int = Field(
        default=45,
        validation_alias="SCRAPER_CRAWLEE_TIMEOUT_SEC",
    )
    crawlee_headless: bool = Field(
        default=True,
        validation_alias="SCRAPER_CRAWLEE_HEADLESS",
    )
    crawlee_max_retries: int = Field(
        default=2,
        validation_alias="SCRAPER_CRAWLEE_MAX_RETRIES",
    )

    direct_html_enabled: bool = Field(
        default=True,
        validation_alias="SCRAPER_DIRECT_HTML_ENABLED",
    )
    direct_html_timeout_sec: int = Field(
        default=30,
        validation_alias="SCRAPER_DIRECT_HTML_TIMEOUT_SEC",
    )
    direct_html_max_response_mb: int = Field(
        default=10,
        validation_alias="SCRAPER_DIRECT_HTML_MAX_RESPONSE_MB",
    )

    direct_pdf_enabled: bool = Field(
        default=True,
        validation_alias="SCRAPER_DIRECT_PDF_ENABLED",
        description="Enable direct PDF download+extraction via PyMuPDF",
    )
    direct_pdf_timeout_sec: int = Field(
        default=60,
        validation_alias="SCRAPER_DIRECT_PDF_TIMEOUT_SEC",
    )
    direct_pdf_max_size_mb: int = Field(
        default=20,
        validation_alias="SCRAPER_DIRECT_PDF_MAX_SIZE_MB",
    )

    crawl4ai_enabled: bool = Field(
        default=True,
        validation_alias="SCRAPER_CRAWL4AI_ENABLED",
    )
    crawl4ai_url: str = Field(
        default="http://crawl4ai:11235",
        validation_alias="SCRAPER_CRAWL4AI_URL",
    )
    crawl4ai_token: str = Field(
        default="",
        validation_alias="SCRAPER_CRAWL4AI_TOKEN",
        json_schema_extra=SECRET_MARKER,
    )
    crawl4ai_timeout_sec: int = Field(
        default=60,
        validation_alias="SCRAPER_CRAWL4AI_TIMEOUT_SEC",
    )
    crawl4ai_cache_mode: str = Field(
        default="BYPASS",
        validation_alias="SCRAPER_CRAWL4AI_CACHE_MODE",
        description="Crawl4AI cache mode: ENABLED, DISABLED, BYPASS, READ_ONLY, WRITE_ONLY",
    )

    playwright_fingerprint_slim: bool = Field(
        default=False,
        validation_alias="SCRAPER_PLAYWRIGHT_FINGERPRINT_SLIM",
        description="Use smaller, lower-overhead fingerprints for the Playwright provider",
    )

    scrapegraph_enabled: bool = Field(
        default=False,
        validation_alias="SCRAPER_SCRAPEGRAPH_ENABLED",
        description=(
            "Enable the ScrapeGraph-AI last-resort provider. Default is off "
            "because the underlying `scrapegraphai` package lives in the "
            "opt-in `scraper_ai` extra; with default-on, installs that did "
            "not opt in emit a 'scrapegraph_ai_import_failed' warning on "
            "every URL the chain has to fall through to."
        ),
    )
    scrapegraph_timeout_sec: int = Field(
        default=90,
        validation_alias="SCRAPER_SCRAPEGRAPH_TIMEOUT_SEC",
    )

    @field_validator("profile", mode="before")
    @classmethod
    def _validate_profile(cls, value: Any) -> str:
        profile = str(value or "balanced").strip().lower()
        if profile not in SCRAPER_PROFILES:
            msg = "SCRAPER_PROFILE must be one of: fast, balanced, robust"
            raise ValueError(msg)
        return profile

    @field_validator("crawl4ai_cache_mode", mode="before")
    @classmethod
    def _validate_crawl4ai_cache_mode(cls, value: Any) -> str:
        allowed = {"ENABLED", "DISABLED", "BYPASS", "READ_ONLY", "WRITE_ONLY"}
        mode = str(value or "BYPASS").strip().upper()
        if mode not in allowed:
            allowed_str = ", ".join(sorted(allowed))
            msg = f"SCRAPER_CRAWL4AI_CACHE_MODE must be one of: {allowed_str}"
            raise ValueError(msg)
        return mode

    @field_validator("force_provider", mode="before")
    @classmethod
    def _validate_force_provider(cls, value: Any) -> str | None:
        if value in (None, ""):
            return None
        provider = str(value).strip().lower()
        if provider not in SCRAPER_PROVIDER_TOKENS:
            allowed = ", ".join(sorted(SCRAPER_PROVIDER_TOKENS))
            msg = f"SCRAPER_FORCE_PROVIDER must be one of: {allowed}"
            raise ValueError(msg)
        return provider

    @field_validator("provider_order", mode="before")
    @classmethod
    def _parse_provider_order(cls, value: Any) -> list[str]:
        if value in (None, ""):
            return list(DEFAULT_SCRAPER_PROVIDER_ORDER)

        raw_items: list[Any]
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return []
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as exc:
                    msg = "SCRAPER_PROVIDER_ORDER must be valid JSON array or CSV"
                    raise ValueError(msg) from exc
                if not isinstance(parsed, list):
                    msg = "SCRAPER_PROVIDER_ORDER JSON value must be an array"
                    raise ValueError(msg)
                raw_items = parsed
            else:
                raw_items = [item.strip() for item in text.split(",")]
        elif isinstance(value, list | tuple):
            raw_items = list(value)
        else:
            msg = "SCRAPER_PROVIDER_ORDER must be a list, JSON array, or CSV string"
            raise ValueError(msg)

        normalized: list[str] = []
        seen: set[str] = set()
        for raw in raw_items:
            token = str(raw).strip().strip('"').strip("'").lower()
            if not token:
                continue
            if token not in SCRAPER_PROVIDER_TOKENS:
                allowed = ", ".join(sorted(SCRAPER_PROVIDER_TOKENS))
                msg = f"Unknown scraper provider '{token}'. Allowed: {allowed}"
                raise ValueError(msg)
            if token in seen:
                msg = f"Duplicate scraper provider '{token}' in SCRAPER_PROVIDER_ORDER"
                raise ValueError(msg)
            seen.add(token)
            normalized.append(token)

        return normalized

    @field_validator("js_heavy_hosts", mode="before")
    @classmethod
    def _parse_js_heavy_hosts(cls, value: Any) -> tuple[str, ...]:
        if value in (None, ""):
            return ()
        if isinstance(value, str):
            hosts = [part.strip().lower() for part in value.split(",") if part.strip()]
            return tuple(sorted(set(hosts)))
        if isinstance(value, list | tuple):
            hosts = [str(part).strip().lower() for part in value if str(part).strip()]
            return tuple(sorted(set(hosts)))
        msg = "SCRAPER_JS_HEAVY_HOSTS must be a CSV string or list"
        raise ValueError(msg)

    @field_validator(
        "min_content_length",
        "scrapling_timeout_sec",
        "defuddle_timeout_sec",
        "firecrawl_timeout_sec",
        "firecrawl_wait_for_ms",
        "firecrawl_max_retries",
        "firecrawl_max_connections",
        "firecrawl_max_keepalive_connections",
        "firecrawl_max_response_size_mb",
        "cloakbrowser_timeout_sec",
        "playwright_timeout_sec",
        "crawlee_timeout_sec",
        "crawlee_max_retries",
        "direct_html_timeout_sec",
        "direct_html_max_response_mb",
        "direct_pdf_timeout_sec",
        "direct_pdf_max_size_mb",
        "crawl4ai_timeout_sec",
        "scrapegraph_timeout_sec",
        mode="before",
    )
    @classmethod
    def _validate_int_fields(cls, value: Any, info: ValidationInfo) -> int:
        default = cls.model_fields[info.field_name].default
        raw = default if value in (None, "") else value
        try:
            parsed = int(str(raw))
        except ValueError as exc:
            msg = f"{info.field_name.replace('_', ' ')} must be a valid integer"
            raise ValueError(msg) from exc

        bounds: dict[str, tuple[int, int]] = {
            "min_content_length": (50, 20_000),
            "scrapling_timeout_sec": (1, 300),
            "defuddle_timeout_sec": (1, 300),
            "firecrawl_timeout_sec": (1, 300),
            "firecrawl_wait_for_ms": (0, 30_000),
            "firecrawl_max_retries": (0, 10),
            "firecrawl_max_connections": (1, 100),
            "firecrawl_max_keepalive_connections": (1, 50),
            "firecrawl_max_response_size_mb": (1, 1024),
            "cloakbrowser_timeout_sec": (1, 300),
            "playwright_timeout_sec": (1, 300),
            "crawlee_timeout_sec": (1, 300),
            "crawlee_max_retries": (0, 10),
            "direct_html_timeout_sec": (1, 300),
            "direct_html_max_response_mb": (1, 200),
            "direct_pdf_timeout_sec": (1, 300),
            "direct_pdf_max_size_mb": (1, 200),
            "crawl4ai_timeout_sec": (1, 300),
            "scrapegraph_timeout_sec": (1, 600),
        }
        min_val, max_val = bounds[info.field_name]
        if parsed < min_val or parsed > max_val:
            msg = (
                f"{info.field_name.replace('_', ' ').capitalize()} must be between "
                f"{min_val} and {max_val}"
            )
            raise ValueError(msg)
        return parsed

    @field_validator("firecrawl_keepalive_expiry", mode="before")
    @classmethod
    def _validate_keepalive_expiry(cls, value: Any) -> float:
        raw = 30.0 if value in (None, "") else value
        try:
            parsed = float(str(raw))
        except ValueError as exc:
            msg = "firecrawl_keepalive_expiry must be a valid number"
            raise ValueError(msg) from exc
        if parsed < 1.0 or parsed > 300.0:
            msg = "firecrawl_keepalive_expiry must be between 1.0 and 300.0"
            raise ValueError(msg)
        return parsed
