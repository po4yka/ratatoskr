"""Hacker News Algolia item API scraper provider."""

from __future__ import annotations

import re
import time
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger, redact_url_for_logging
from app.security.ssrf import make_safe_async_client

logger = get_logger(__name__)

_ITEM_ID_RE = re.compile(r"^\d+$")
_HN_HOSTS = {"news.ycombinator.com", "www.news.ycombinator.com", "hn.algolia.com"}
_HTML_BREAK_RE = re.compile(r"<\s*(br|/p|/div)\s*/?\s*>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_BLANK_LINE_RE = re.compile(r"\n{3,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.;:!?])")


class HackerNewsProvider:
    """Extract Hacker News stories and comments through Algolia's public item API."""

    def __init__(
        self,
        *,
        timeout_sec: int = 20,
        top_comments: int = 20,
        client: Any | None = None,
    ) -> None:
        self._timeout_sec = timeout_sec
        self._top_comments = max(1, top_comments)
        self._client = client
        self._owns_client = client is None

    @property
    def provider_name(self) -> str:
        return "hn"

    def supports_url(self, url: str) -> bool:
        return _extract_item_id(url) is not None

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        del mobile, request_id
        started = time.perf_counter()
        item_id = _extract_item_id(url)
        if item_id is None:
            return _error_result(url, "Hacker News provider does not support this URL", started)

        endpoint = f"https://hn.algolia.com/api/v1/items/{item_id}"
        try:
            payload, http_status = await self._fetch_json(endpoint)
            markdown, metadata = _render_hn_markdown(payload, url, self._top_comments)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.debug(
                "hn_provider_failed",
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
                error_text=f"Hacker News scrape failed: {exc}",
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
                "platform": "hackernews",
                "top_comments": self._top_comments,
            },
        )

    async def _fetch_json(self, endpoint: str) -> tuple[dict[str, Any], int]:
        client = self._client
        if client is None:
            client = make_safe_async_client(timeout=self._timeout_sec, follow_redirects=True)
            async with client as owned_client:
                response = await owned_client.get(endpoint, headers={"Accept": "application/json"})
                response.raise_for_status()
                return response.json(), response.status_code

        response = await client.get(endpoint, headers={"Accept": "application/json"})
        response.raise_for_status()
        return response.json(), response.status_code

    async def aclose(self) -> None:
        if self._owns_client or self._client is None:
            return
        aclose = getattr(self._client, "aclose", None)
        if aclose is not None:
            await aclose()


def _extract_item_id(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in _HN_HOSTS:
        return None

    query_id = parse_qs(parsed.query).get("id", [None])[0]
    if query_id and _ITEM_ID_RE.fullmatch(query_id):
        return query_id

    path_parts = [part for part in parsed.path.split("/") if part]
    if (
        host == "hn.algolia.com"
        and len(path_parts) >= 3
        and path_parts[:3] == ["api", "v1", "items"]
    ):
        candidate = path_parts[3] if len(path_parts) > 3 else None
        if candidate and _ITEM_ID_RE.fullmatch(candidate):
            return candidate
    return None


def _render_hn_markdown(
    payload: dict[str, Any], source_url: str, top_comments: int
) -> tuple[str, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("unexpected Hacker News payload")

    item_id = payload.get("id")
    title = _clean_text(payload.get("title")) or "Hacker News discussion"
    story_url = _clean_text(payload.get("url"))
    author = _clean_text(payload.get("author"))
    points = payload.get("points")
    created_at = _clean_text(payload.get("created_at"))
    story_text = _clean_html_text(payload.get("text"))

    lines = [
        f"# {title}",
        "",
        f"Source: {source_url}",
        f"HN item: https://news.ycombinator.com/item?id={item_id}",
    ]
    if story_url:
        lines.append(f"Linked URL: {story_url}")
    if author:
        lines.append(f"Author: {author}")
    if points is not None:
        lines.append(f"Points: {points}")
    if created_at:
        lines.append(f"Created: {created_at}")

    if story_text:
        lines.extend(["", "## Story text", "", story_text])

    comments = _flatten_comments(payload.get("children"), top_comments)
    if comments:
        lines.extend(["", "## Comments"])
        for index, comment in enumerate(comments, start=1):
            comment_author = _clean_text(comment.get("author")) or "unknown"
            comment_text = _clean_html_text(comment.get("text"))
            if not comment_text:
                continue
            lines.extend(["", f"### {index}. {comment_author}", "", comment_text])

    markdown = "\n".join(lines).strip()
    if len(markdown) < 50:
        raise ValueError("Hacker News payload produced no usable markdown")

    return markdown, {
        "provider": "hn",
        "item_id": item_id,
        "title": title,
        "linked_url": story_url,
        "top_comments_count": len(comments),
    }


def _flatten_comments(value: Any, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    comments: list[dict[str, Any]] = []
    stack = list(reversed(value))
    while stack and len(comments) < limit:
        comment = stack.pop()
        if not isinstance(comment, dict):
            continue
        if _clean_html_text(comment.get("text")):
            comments.append(comment)
        children = comment.get("children")
        if isinstance(children, list):
            stack.extend(child for child in reversed(children) if isinstance(child, dict))
    return comments


def _clean_html_text(value: Any) -> str:
    raw = _clean_text(value)
    if not raw:
        return ""
    text = _HTML_BREAK_RE.sub("\n", raw)
    text = _HTML_TAG_RE.sub(" ", text)
    text = unescape(text)
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return _BLANK_LINE_RE.sub("\n\n", "\n".join(line for line in lines if line)).strip()


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
        endpoint="hn",
    )
