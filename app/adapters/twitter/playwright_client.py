"""Playwright-based browser automation for Twitter/X content extraction.

Wraps sync Playwright calls in asyncio.to_thread() for async compatibility.
Lazy-imports playwright to fail gracefully when not installed.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote, urljoin, urlparse

from app.core.logging_utils import get_logger
from app.security.ssrf import is_url_safe_async, make_safe_async_client

if TYPE_CHECKING:
    from pathlib import Path

from app.adapters.twitter.graphql_parser import (
    ExtractionResult,
    TweetData,
    extract_tweets_from_graphql,
)

logger = get_logger(__name__)

# Limit concurrent Chromium processes to avoid OOM on memory-constrained hosts
# (e.g. Raspberry Pi).  Configured via TWITTER_MAX_CONCURRENT_BROWSERS; default 1.
_playwright_sem: asyncio.Semaphore | None = None


def _get_playwright_sem() -> asyncio.Semaphore:
    """Return the module-level semaphore, creating it on first use."""
    global _playwright_sem
    if _playwright_sem is None:
        from app.config.twitter import TwitterConfig

        try:
            cfg = TwitterConfig()
            limit = cfg.max_concurrent_browsers
        except Exception:
            limit = 1
        _playwright_sem = asyncio.Semaphore(limit)
    return _playwright_sem


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_ARTICLE_EXPAND_BUTTONS_SCRIPT = """(labels) => {
    const normalizedLabels = new Set(labels.map(x => x.toLowerCase()));
    const container = document.querySelector('article') || document.querySelector('main');
    if (!container) return;
    const buttons = container.querySelectorAll('button, [role="button"]');
    buttons.forEach((btn) => {
        const text = (btn.innerText || btn.textContent || '').trim().toLowerCase();
        if (!text) return;
        for (const label of normalizedLabels) {
            if (text === label || text.includes(label)) {
                btn.click();
                break;
            }
        }
    });
}"""

_ARTICLE_SCRAPE_SCRIPT = """() => {
    const result = {
        title: '',
        author: '',
        authorHandle: '',
        content: '',
        images: [],
        finalUrl: window.location.href || '',
        canonicalUrl: '',
        selectorFallbackUsed: false,
        contentSelector: 'unknown',
    };

    const canonicalEl = document.querySelector('link[rel="canonical"]');
    if (canonicalEl && canonicalEl.href) result.canonicalUrl = canonicalEl.href;
    if (!result.canonicalUrl) {
        const ogUrl = document.querySelector('meta[property="og:url"]');
        if (ogUrl && ogUrl.content) result.canonicalUrl = ogUrl.content;
    }

    const titleSelectors = [
        'article h1',
        '[data-testid="article-cover-title"]',
        'main h1',
    ];
    for (const sel of titleSelectors) {
        const el = document.querySelector(sel);
        const value = (el?.innerText || '').trim();
        if (value) {
            result.title = value;
            break;
        }
    }
    if (!result.title) {
        const ogTitle = document.querySelector('meta[property="og:title"]');
        if (ogTitle && ogTitle.content) result.title = ogTitle.content;
    }

    const authorLink = document.querySelector(
        '[data-testid="User-Name"] a[href*="/"], article a[href*="/"], main a[href*="/"]'
    );
    const authorText = (authorLink?.innerText || '').trim();
    if (authorText) result.author = authorText;
    const href = (authorLink?.getAttribute('href') || '').trim();
    const handleMatch = href.match(/^\\/@?([A-Za-z0-9_]{1,32})$/);
    if (handleMatch) result.authorHandle = handleMatch[1];

    const articleEl = document.querySelector('article');
    const mainEl = document.querySelector('main [data-testid="primaryColumn"], main');
    const bodyEl = document.body;
    const contentEl = articleEl || mainEl || bodyEl;
    result.contentSelector = articleEl ? 'article' : (mainEl ? 'main' : 'body');
    result.selectorFallbackUsed = result.contentSelector !== 'article';
    result.content = (contentEl?.innerText || '').trim();

    const imageEls = contentEl?.querySelectorAll('img[src]') || [];
    imageEls.forEach((img) => {
        const src = (img.getAttribute('src') || '').trim();
        if (!src || src.startsWith('data:')) return;
        if (!result.images.includes(src)) result.images.push(src);
    });

    return result;
}"""


def _load_cookies_netscape(cookies_path: Path) -> list[dict[str, Any]]:
    """Parse a Netscape-format cookies.txt file into Playwright cookie dicts."""
    cookies: list[dict[str, Any]] = []
    for line in cookies_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line.removeprefix("#HttpOnly_")
        elif line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, _flag, path, secure, expires, name, value = parts[:7]
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path,
                "secure": secure.upper() == "TRUE",
                "expires": int(expires) if expires != "0" else -1,
                "httpOnly": http_only,
            }
        )
    return cookies


def _as_playwright_cookies(cookies: list[dict[str, Any]]) -> Any:
    """Convert parsed cookie dicts for Playwright context cookie injection."""
    return cast("Any", cookies)


def _extract_tweet_sync(
    url: str,
    cookies_path: Path | None = None,
    headless: bool = True,
    timeout_ms: int = 15000,
    expected_tweet_id: str | None = None,
) -> ExtractionResult:
    """Extract tweet data by intercepting X's GraphQL API via Playwright.

    This is a synchronous function -- call via asyncio.to_thread().

    Raises:
        ImportError: If playwright is not installed.
    """
    try:
        from playwright.sync_api import (
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
            sync_playwright,
        )
    except ImportError as exc:
        msg = (
            "Playwright is required for Twitter extraction. "
            "Install with: pip install 'playwright>=1.40' && playwright install chromium"
        )
        raise ImportError(msg) from exc

    captured_responses: list[dict[str, Any]] = []

    def _on_response(response: Any) -> None:
        try:
            if (
                "TweetDetail" in response.url
                and response.status == 200
                and _response_matches_requested_tweet(response.url, expected_tweet_id)
            ):
                captured_responses.append(response.json())
        except (TypeError, ValueError):
            logger.debug("tweet_graphql_response_parse_failed", exc_info=True)
            return

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=_USER_AGENT)

        if cookies_path and cookies_path.exists():
            cookies = _load_cookies_netscape(cookies_path)
            if cookies:
                context.add_cookies(_as_playwright_cookies(cookies))

        page = context.new_page()
        page.on("response", _on_response)

        try:
            page_load_failed = False
            try:
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except (PlaywrightTimeoutError, PlaywrightError):
                page_load_failed = True
                logger.debug("tweet_page_goto_failed_partial_capture_mode", exc_info=True)

            # Scroll once to trigger thread loading (skip when initial load failed)
            if not page_load_failed:
                page.wait_for_timeout(2000)
                page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
                page.wait_for_timeout(2000)
            else:
                page.wait_for_timeout(1000)
        finally:
            # Guarantee the launched browser is torn down even if a post-goto
            # step (scroll/evaluate/wait) raises -- otherwise the Chromium
            # process leaks. Mirrors _scrape_article_sync's cleanup.
            page.close()
            browser.close()

    all_tweets = _merge_captured_tweets(captured_responses)

    return ExtractionResult(url=url, tweets=all_tweets)


def _scrape_article_sync(
    url: str,
    cookies_path: Path | None = None,
    headless: bool = True,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Extract an X Article by rendering in browser and scraping DOM.

    This is a synchronous function -- call via asyncio.to_thread().
    """
    try:
        from playwright.sync_api import (
            Error as PlaywrightError,
            TimeoutError as PlaywrightTimeoutError,
            sync_playwright,
        )
    except ImportError as exc:
        msg = (
            "Playwright is required for Twitter extraction. "
            "Install with: pip install 'playwright>=1.40' && playwright install chromium"
        )
        raise ImportError(msg) from exc

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(user_agent=_USER_AGENT)

        if cookies_path and cookies_path.exists():
            cookies = _load_cookies_netscape(cookies_path)
            if cookies:
                context.add_cookies(_as_playwright_cookies(cookies))

        page = context.new_page()
        try:
            page_load_failed = False
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            except (PlaywrightTimeoutError, PlaywrightError):
                page_load_failed = True
                logger.debug("article_page_goto_failed", exc_info=True)

            # Prefer locator-based readiness before falling back to scripted scraping.
            try:
                page.locator("article").first.wait_for(
                    state="visible", timeout=max(2_000, timeout_ms // 2)
                )
            except (PlaywrightTimeoutError, PlaywrightError):
                page_load_failed = True
                try:
                    page.locator("main").first.wait_for(
                        state="visible",
                        timeout=max(2_000, timeout_ms // 3),
                    )
                except (PlaywrightTimeoutError, PlaywrightError):
                    page_load_failed = True
                    logger.debug("article_readiness_probe_failed", exc_info=True)

            for _ in range(8):
                page.evaluate("window.scrollBy(0, window.innerHeight * 1.5)")
                page.wait_for_timeout(250)

            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
            if page_load_failed:
                page.wait_for_timeout(200)

            expand_labels = [
                "show more",
                "read more",
                "показать",
                "читать дальше",
                "читать далее",
                "mostrar más",
                "voir plus",
                "mehr anzeigen",
            ]
            page.evaluate(_ARTICLE_EXPAND_BUTTONS_SCRIPT, expand_labels)
            page.wait_for_timeout(300)
            return cast("dict[str, Any]", page.evaluate(_ARTICLE_SCRAPE_SCRIPT))
        finally:
            page.close()
            browser.close()


_TCO_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})
_TCO_MAX_REDIRECTS = 5


async def resolve_tco_url(short_url: str, timeout: int = 10) -> str | None:
    """Follow a t.co redirect and return the resolved URL, SSRF-checked per hop.

    t.co links are authored by untrusted third parties, so the redirect chain
    is followed manually -- ``make_safe_async_client`` with redirect following
    disabled, plus an ``is_url_safe_async`` check before every hop -- instead of
    letting httpx follow redirects straight into an internal address (cloud
    metadata endpoint, a compose-network service, loopback). Any destination
    parsed from the HTML fallback is re-validated before it is returned and
    spliced back into tweet text. Mirrors ``resolve_twitter_article_link``.

    Args:
        short_url: URL starting with https://t.co/
        timeout: HTTP request timeout in seconds

    Returns:
        Resolved (safe, non-t.co) URL string, or None if not a t.co URL,
        resolution fails, or the destination is blocked by the SSRF policy.
    """
    parsed = urlparse(short_url)
    host = (parsed.hostname or "").lower()
    scheme = (parsed.scheme or "").lower()
    if host != "t.co" or scheme not in {"http", "https"}:
        return None
    try:
        async with make_safe_async_client(follow_redirects=False, timeout=timeout) as client:
            current_url = short_url
            last_resp = None
            for _ in range(_TCO_MAX_REDIRECTS):
                safe, _reason = await is_url_safe_async(current_url)
                if not safe:
                    return None
                last_resp = await client.head(current_url)
                if last_resp.status_code in _TCO_REDIRECT_STATUS:
                    location = last_resp.headers.get("location")
                    if not location:
                        break
                    current_url = urljoin(current_url, location)
                    continue
                break

            if last_resp is None:
                return None

            if (urlparse(current_url).hostname or "").lower() != "t.co":
                # Escaped t.co. Re-validate to also cover the hop-cap exit path,
                # where current_url is the next (not-yet-fetched) hop.
                safe, _reason = await is_url_safe_async(current_url)
                return current_url if safe else None

            # Still on t.co (JS/meta-refresh redirect): GET and parse the HTML.
            safe, _reason = await is_url_safe_async(current_url)
            if not safe:
                return None
            get_resp = await client.get(current_url)
            destination = _parse_tco_html_redirect(get_resp.text)
            if not destination:
                return None
            safe, _reason = await is_url_safe_async(destination)
            return destination if safe else None
    except Exception:
        return None


def _parse_tco_html_redirect(html: str) -> str | None:
    """Extract destination URL from a t.co HTML redirect page.

    Handles three patterns used by t.co when HTTP 3xx is not available:
    1. ``<meta ... URL=...>`` (meta-refresh)
    2. ``location.replace("...")`` (JavaScript redirect)
    3. ``<title>http...</title>`` (URL-in-title fallback)
    """
    import re

    # Method 1: meta-refresh
    meta_match = re.search(r'<meta[^>]*?URL=(["\']?)([^"\'\s>]+)', html, re.IGNORECASE)
    if meta_match:
        return meta_match.group(2)

    # Method 2: location.replace()
    loc_match = re.search(r'location\.replace\(["\']([^"\']+)', html)
    if loc_match:
        return loc_match.group(1).replace("\\/", "/")

    # Method 3: URL in <title> tag
    title_match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if title_match:
        title = title_match.group(1).strip()
        if title.startswith("http"):
            return title

    return None


async def extract_tweet(
    url: str,
    cookies_path: Path | None = None,
    headless: bool = True,
    timeout_ms: int = 15000,
    expected_tweet_id: str | None = None,
) -> ExtractionResult:
    """Async wrapper around sync Playwright tweet extraction."""
    async with _get_playwright_sem():
        return await asyncio.to_thread(
            _extract_tweet_sync,
            url,
            cookies_path=cookies_path,
            headless=headless,
            timeout_ms=timeout_ms,
            expected_tweet_id=expected_tweet_id,
        )


async def scrape_article(
    url: str,
    cookies_path: Path | None = None,
    headless: bool = True,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Async wrapper around sync Playwright article extraction."""
    async with _get_playwright_sem():
        return await asyncio.to_thread(
            _scrape_article_sync,
            url,
            cookies_path=cookies_path,
            headless=headless,
            timeout_ms=timeout_ms,
        )


def _response_matches_requested_tweet(
    response_url: str,
    expected_tweet_id: str | None,
) -> bool:
    """Check whether a captured TweetDetail response likely belongs to the requested tweet."""
    if not expected_tweet_id:
        return True
    # GraphQL URLs include encoded variables with focalTweetId.
    return expected_tweet_id in unquote(response_url)


def _merge_captured_tweets(captured_responses: list[dict[str, Any]]) -> list[TweetData]:
    """Merge tweets extracted from multiple GraphQL responses in stable global order."""
    staged: list[tuple[int, int, TweetData]] = []
    seen_ids: set[str] = set()

    for response_index, resp_json in enumerate(captured_responses):
        for tweet in extract_tweets_from_graphql(resp_json):
            if tweet.tweet_id in seen_ids:
                continue
            seen_ids.add(tweet.tweet_id)
            staged.append((response_index, tweet.order, tweet))

    staged.sort(key=lambda item: (item[0], item[1]))
    merged = [tweet for _response_index, _local_order, tweet in staged]
    for global_order, tweet in enumerate(merged):
        tweet.order = global_order
    return merged
