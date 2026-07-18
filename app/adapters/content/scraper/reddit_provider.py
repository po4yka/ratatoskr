"""Reddit comments API scraper provider."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.adapters.content.scraper.json_fetch import read_json_capped
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.security.ssrf import make_safe_async_client

logger = get_logger(__name__)

_POST_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")
_REDDIT_HOSTS = {
    "reddit.com",
    "www.reddit.com",
    "old.reddit.com",
    "new.reddit.com",
    "m.reddit.com",
    "np.reddit.com",
    "api.reddit.com",
}


class RedditProvider:
    """Extract Reddit submissions and top comments through Reddit's public JSON API."""

    def __init__(
        self,
        *,
        timeout_sec: int = 20,
        top_comments: int = 5,
        user_agent: str,
        client: Any | None = None,
        max_response_mb: int = 10,
    ) -> None:
        self._timeout_sec = timeout_sec
        self._top_comments = max(1, top_comments)
        self._user_agent = user_agent
        self._client = client
        self._owns_client = client is None
        self._max_response_bytes = max_response_mb * 1024 * 1024

    @property
    def provider_name(self) -> str:
        return "reddit"

    def supports_url(self, url: str) -> bool:
        return _extract_post_id(url) is not None

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        del mobile, request_id
        started = time.perf_counter()
        post_id = _extract_post_id(url)
        if post_id is None:
            return _error_result(url, "Reddit provider does not support this URL", started)

        endpoint = (
            f"https://www.reddit.com/comments/{post_id}.json"
            f"?limit={self._top_comments}&sort=top&raw_json=1"
        )
        try:
            payload, http_status = await self._fetch_json(endpoint)
            markdown, metadata = _render_reddit_markdown(payload, url, self._top_comments)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.debug(
                "reddit_provider_failed",
                extra={
                    "url": redact_url_for_logging(url),
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                http_status=getattr(exc, "response", None).status_code
                if getattr(exc, "response", None) is not None
                else None,
                error_text=f"Reddit scrape failed: {exc}",
                latency_ms=latency,
                source_url=url,
                endpoint=self.provider_name,
                options_json={"api_endpoint": endpoint, "top_comments": self._top_comments},
            )

        latency = int((time.perf_counter() - started) * 1000)
        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=http_status,
            content_markdown=markdown,
            latency_ms=latency,
            source_url=url,
            endpoint=self.provider_name,
            metadata_json=metadata,
            options_json={
                "api_endpoint": endpoint,
                "platform": "reddit",
                "top_comments": self._top_comments,
            },
        )

    async def _fetch_json(self, endpoint: str) -> tuple[Any, int]:
        if self._client is None:
            client = make_safe_async_client(timeout=self._timeout_sec, follow_redirects=True)
            async with client as owned_client:
                return await read_json_capped(
                    owned_client,
                    endpoint,
                    headers=self._headers(),
                    max_bytes=self._max_response_bytes,
                )

        return await read_json_capped(
            self._client,
            endpoint,
            headers=self._headers(),
            max_bytes=self._max_response_bytes,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "User-Agent": self._user_agent,
        }

    async def aclose(self) -> None:
        if self._owns_client or self._client is None:
            return
        aclose = getattr(self._client, "aclose", None)
        if aclose is not None:
            await aclose()


def _extract_post_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]

    if host == "redd.it" and path_parts:
        candidate = path_parts[0].removesuffix(".json")
        return candidate if _POST_ID_RE.fullmatch(candidate) else None

    if host not in _REDDIT_HOSTS:
        return None

    if len(path_parts) >= 2 and path_parts[0] == "comments":
        candidate = path_parts[1].removesuffix(".json")
        return candidate if _POST_ID_RE.fullmatch(candidate) else None

    if len(path_parts) >= 4 and path_parts[0] == "r" and path_parts[2] == "comments":
        candidate = path_parts[3].removesuffix(".json")
        return candidate if _POST_ID_RE.fullmatch(candidate) else None

    query_id = parse_qs(parsed.query).get("id", [None])[0]
    if query_id and _POST_ID_RE.fullmatch(query_id):
        return query_id
    return None


def _render_reddit_markdown(
    payload: Any, source_url: str, top_comments: int
) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, list) or len(payload) < 2:
        raise ValueError("unexpected Reddit comments payload")

    post_listing = payload[0]
    comments_listing = payload[1]
    post_children = _children(post_listing)
    if not post_children:
        raise ValueError("Reddit payload has no submission")

    post = post_children[0].get("data") or {}
    title = _clean_text(post.get("title")) or "Reddit discussion"
    subreddit = _clean_text(post.get("subreddit"))
    author = _clean_text(post.get("author"))
    selftext = _clean_text(post.get("selftext"))
    outbound_url = _clean_text(post.get("url_overridden_by_dest") or post.get("url"))
    score = post.get("score")
    num_comments = post.get("num_comments")

    lines = [f"# {title}", "", f"Source: {source_url}"]
    if subreddit:
        lines.append(f"Subreddit: r/{subreddit}")
    if author:
        lines.append(f"Author: u/{author}")
    if score is not None:
        lines.append(f"Score: {score}")
    if num_comments is not None:
        lines.append(f"Comments: {num_comments}")

    lines.extend(["", "## Original post"])
    if selftext:
        lines.extend(["", selftext])
    elif outbound_url and outbound_url != source_url:
        lines.extend(["", f"Link: {outbound_url}"])
    else:
        lines.extend(["", "(No original post body.)"])

    comments = _top_comments(_children(comments_listing), top_comments)
    if comments:
        lines.extend(["", "## Top replies"])
        for index, comment in enumerate(comments, start=1):
            data = comment.get("data") or {}
            comment_author = _clean_text(data.get("author")) or "unknown"
            comment_score = data.get("score")
            body = _clean_text(data.get("body"))
            if not body:
                continue
            heading = f"### {index}. u/{comment_author}"
            if comment_score is not None:
                heading += f" ({comment_score} points)"
            lines.extend(["", heading, "", body])

    markdown = "\n".join(lines).strip()
    if len(markdown) < 50:
        raise ValueError("Reddit payload produced no usable markdown")

    return markdown, {
        "provider": "reddit",
        "post_id": post.get("id"),
        "subreddit": subreddit,
        "title": title,
        "top_comments_count": len(comments),
    }


def _children(listing: Any) -> list[dict[str, Any]]:
    if not isinstance(listing, dict):
        return []
    data = listing.get("data")
    if not isinstance(data, dict):
        return []
    children = data.get("children")
    return children if isinstance(children, list) else []


def _top_comments(children: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    comments: list[dict[str, Any]] = []
    stack = list(children)
    while stack and len(comments) < limit:
        child = stack.pop(0)
        if child.get("kind") != "t1":
            continue
        data = child.get("data") or {}
        body = _clean_text(data.get("body"))
        if body and body.lower() not in {"[deleted]", "[removed]"}:
            comments.append(child)
        replies = data.get("replies")
        if isinstance(replies, dict):
            stack.extend(_children(replies))
    return comments


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _error_result(url: str, message: str, started: float) -> FirecrawlResult:
    return FirecrawlResult(
        status=CallStatus.ERROR,
        error_text=message,
        latency_ms=int((time.perf_counter() - started) * 1000),
        source_url=url,
        endpoint="reddit",
    )
