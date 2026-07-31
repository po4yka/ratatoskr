"""Firecrawl v2 endpoint constants."""

FIRECRAWL_BASE_URL = "https://api.firecrawl.dev"
FIRECRAWL_SCRAPE_ENDPOINT = "/v2/scrape"
FIRECRAWL_SEARCH_ENDPOINT = "/v2/search"
FIRECRAWL_CRAWL_ENDPOINT = "/v2/crawl"
FIRECRAWL_EXTRACT_ENDPOINT = "/v2/extract"

FIRECRAWL_SCRAPE_URL = f"{FIRECRAWL_BASE_URL}{FIRECRAWL_SCRAPE_ENDPOINT}"
FIRECRAWL_SEARCH_URL = f"{FIRECRAWL_BASE_URL}{FIRECRAWL_SEARCH_ENDPOINT}"
FIRECRAWL_CRAWL_URL = f"{FIRECRAWL_BASE_URL}{FIRECRAWL_CRAWL_ENDPOINT}"
FIRECRAWL_EXTRACT_URL = f"{FIRECRAWL_BASE_URL}{FIRECRAWL_EXTRACT_ENDPOINT}"


def build_urls(base: str) -> dict[str, str]:
    """Build all Firecrawl endpoint URLs from a custom base URL."""
    base = base.rstrip("/")
    return {
        "scrape": f"{base}{FIRECRAWL_SCRAPE_ENDPOINT}",
        "search": f"{base}{FIRECRAWL_SEARCH_ENDPOINT}",
        "crawl": f"{base}{FIRECRAWL_CRAWL_ENDPOINT}",
        "extract": f"{base}{FIRECRAWL_EXTRACT_ENDPOINT}",
    }
