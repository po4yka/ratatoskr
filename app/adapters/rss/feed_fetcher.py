"""RSS/Atom feed fetcher."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlparse

from app.core.logging_utils import get_logger
from app.security.ssrf import is_url_safe, make_safe_sync_client

logger = get_logger(__name__)


def _validate_feed_url(url: str) -> None:
    """Validate that *url* points to a public host. Raises ``ValueError`` on failure."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Blocked URL scheme: {parsed.scheme}")

    safe, reason = is_url_safe(url)
    if not safe:
        raise ValueError(f"Feed URL blocked: {reason}")


# Feeds are text; anything this large is a broken or hostile server. The
# sibling source ingestors cap their responses the same way -- this fetcher
# buffered `resp.content` with no bound at all, and POST /rss accepts an
# arbitrary URL at subscription time.
_MAX_FEED_BYTES = 10 * 1024 * 1024
# Feedburner and Substack answer 301 to their canonical https host. With
# follow_redirects=False and only 304 special-cased, raise_for_status turned
# that into a failure, and roughly ten polls (~5 h) later the feed auto-disabled.
_MAX_FEED_REDIRECTS = 3


def _read_capped(response: Any) -> bytes:
    """Read a streamed body, refusing anything over the cap.

    Content-Length is the cheap early-out; the cumulative count is the
    authoritative one because the header can be absent or wrong.
    """
    declared = response.headers.get("content-length")
    if declared:
        try:
            if int(declared) > _MAX_FEED_BYTES:
                msg = f"feed body exceeds {_MAX_FEED_BYTES} byte cap"
                raise ValueError(msg)
        except ValueError as exc:
            if "cap" in str(exc):
                raise

    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > _MAX_FEED_BYTES:
            msg = f"feed body exceeds {_MAX_FEED_BYTES} byte cap"
            raise ValueError(msg)
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_feed_body(
    url: str, *, headers: dict[str, str], timeout: float
) -> tuple[bytes, dict[str, str]] | None:
    """Fetch the feed, following a bounded number of redirects.

    Returns ``(body, response_headers)``, or None when the server answers 304.
    The headers come back because ETag / Last-Modified drive the conditional
    request on the next poll. Every redirect target is re-validated, so
    following them does not widen the SSRF surface.
    """
    current_url = url
    with make_safe_sync_client(follow_redirects=False) as client:
        for _ in range(_MAX_FEED_REDIRECTS + 1):
            with client.stream("GET", current_url, headers=headers, timeout=timeout) as resp:
                if resp.status_code == 304:
                    return None
                location = resp.headers.get("location")
                if resp.status_code in {301, 302, 303, 307, 308} and location:
                    current_url = urljoin(current_url, location)
                    _validate_feed_url(current_url)
                    continue
                resp.raise_for_status()
                # Lower-cased explicitly: httpx normalises its own Headers, but the
                # mapping is rebuilt here so lookups do not depend on which
                # casing the server (or a test double) used.
                return _read_capped(resp), {
                    key.lower(): value for key, value in dict(resp.headers).items()
                }
    msg = f"feed exceeded {_MAX_FEED_REDIRECTS} redirects"
    raise ValueError(msg)


@dataclass
class FeedEntry:
    guid: str
    title: str | None = None
    url: str | None = None
    content: str | None = None
    author: str | None = None
    published_at: datetime | None = None


@dataclass
class FeedResult:
    title: str | None = None
    description: str | None = None
    site_url: str | None = None
    entries: list[FeedEntry] = field(default_factory=list)
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


def fetch_feed(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    timeout: float = 30.0,
) -> FeedResult:
    """Fetch and parse an RSS/Atom feed.

    Uses conditional headers (ETag, Last-Modified) to avoid re-downloading unchanged feeds.
    Returns FeedResult with not_modified=True if server returns 304.
    """
    # SSRF protection: block requests to internal/private networks
    _validate_feed_url(url)

    headers: dict[str, str] = {"User-Agent": "Ratatoskr-FeedFetcher/1.0"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    fetched = _fetch_feed_body(url, headers=headers, timeout=timeout)
    if fetched is None:
        return FeedResult(not_modified=True)
    body, response_headers = fetched

    import feedparser

    parsed = feedparser.parse(body)

    entries = []
    for entry in parsed.entries:
        guid = entry.get("id") or entry.get("link") or entry.get("title", "")
        pub_date = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            try:
                import calendar

                pub_date = datetime.fromtimestamp(calendar.timegm(entry.published_parsed), tz=UTC)
            except (ValueError, OverflowError, TypeError):
                logger.debug(
                    "rss_date_parse_failed",
                    extra={"guid": guid, "raw": str(entry.published_parsed)},
                )

        content_text = ""
        if entry.get("content"):
            content_text = entry.content[0].get("value", "")
        elif entry.get("summary"):
            content_text = entry.summary

        entries.append(
            FeedEntry(
                guid=guid,
                title=entry.get("title"),
                url=entry.get("link"),
                content=content_text or None,
                author=entry.get("author"),
                published_at=pub_date,
            )
        )

    feed_info = parsed.feed
    return FeedResult(
        title=feed_info.get("title"),
        description=feed_info.get("subtitle") or feed_info.get("description"),
        site_url=feed_info.get("link"),
        entries=entries,
        etag=response_headers.get("etag"),
        last_modified=response_headers.get("last-modified"),
    )
