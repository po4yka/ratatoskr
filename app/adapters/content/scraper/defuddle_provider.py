"""Defuddle (self-hosted) HTTP content extraction provider."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urljoin

import httpx

from app.adapters.content.scraper.target_safety import reject_unsafe_target_url
from app.adapters.external.firecrawl.models import FirecrawlResult
from app.core.call_status import CallStatus
from app.core.logging_utils import get_logger
from app.security.ssrf import is_url_safe, make_safe_async_client

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SEC = 20
_DEFAULT_API_BASE_URL = "http://defuddle-api:3003"
_FRONTMATTER_MARKER = "---"

_HEADERS = {
    "Accept": "text/plain,text/markdown,*/*;q=0.8",
    "User-Agent": "Mozilla/5.0 (compatible; BiteSize/1.0)",
}


class DefuddleProvider:
    """Content extraction via the Defuddle HTTP API (defuddle.md/<url>).

    Returns clean Markdown with YAML frontmatter parsed into metadata_json.
    """

    def __init__(
        self,
        timeout_sec: int = _DEFAULT_TIMEOUT_SEC,
        *,
        min_content_length: int = 400,
        api_base_url: str = _DEFAULT_API_BASE_URL,
        api_token: str = "",
    ) -> None:
        self._timeout_sec = timeout_sec
        self._min_content_length = min_content_length
        self._api_base_url = api_base_url.rstrip("/")
        self._api_token = api_token
        if self._api_base_url.lower().rstrip("/") == "https://defuddle.md":
            logger.warning(
                "defuddle_provider_cloud_url_deprecated",
                extra={"url": api_base_url},
            )

    @property
    def provider_name(self) -> str:
        return "defuddle"

    async def scrape_markdown(
        self,
        url: str,
        *,
        mobile: bool = True,
        request_id: int | None = None,
    ) -> FirecrawlResult:
        del mobile  # Defuddle API does not expose mobile/desktop distinction
        started = time.perf_counter()
        unsafe_result = await reject_unsafe_target_url(
            provider="defuddle",
            url=url,
            started=started,
            request_id=request_id,
        )
        if unsafe_result is not None:
            return unsafe_result

        try:
            raw_body = await self._fetch_raw(url)
        except TimeoutError:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "defuddle_timeout",
                extra={"url": url, "timeout_sec": self._timeout_sec, "request_id": request_id},
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Defuddle timeout after {self._timeout_sec}s",
                latency_ms=latency,
                source_url=url,
                endpoint="defuddle",
            )
        except httpx.HTTPStatusError as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.warning(
                "defuddle_http_error",
                extra={
                    "url": url,
                    "status_code": exc.response.status_code,
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Defuddle HTTP {exc.response.status_code}",
                http_status=exc.response.status_code,
                latency_ms=latency,
                source_url=url,
                endpoint="defuddle",
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            logger.debug(
                "defuddle_fetch_failed",
                extra={
                    "url": url,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=f"Defuddle fetch failed: {exc}",
                latency_ms=latency,
                source_url=url,
                endpoint="defuddle",
            )

        latency = int((time.perf_counter() - started) * 1000)

        if not raw_body or not raw_body.strip():
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text="Defuddle: empty response",
                latency_ms=latency,
                source_url=url,
                endpoint="defuddle",
            )

        metadata, markdown = _parse_frontmatter(raw_body)

        if not markdown or len(markdown.strip()) < self._min_content_length:
            logger.info(
                "defuddle_thin_content",
                extra={
                    "url": url,
                    "content_len": len(markdown.strip()) if markdown else 0,
                    "threshold": self._min_content_length,
                    "request_id": request_id,
                },
            )
            return FirecrawlResult(
                status=CallStatus.ERROR,
                error_text=(
                    f"Defuddle: content too short "
                    f"({len(markdown.strip()) if markdown else 0} chars)"
                ),
                latency_ms=latency,
                source_url=url,
                endpoint="defuddle",
            )

        return FirecrawlResult(
            status=CallStatus.OK,
            http_status=200,
            content_markdown=markdown.strip(),
            metadata_json=metadata or None,
            latency_ms=latency,
            source_url=url,
            endpoint="defuddle",
        )

    async def _fetch_raw(self, url: str) -> str:
        defuddle_url = f"{self._api_base_url}/{url}"
        headers = dict(_HEADERS)
        if self._api_token:
            headers["Authorization"] = f"Bearer {self._api_token}"
        overall_timeout = self._timeout_sec + 5
        async with asyncio.timeout(overall_timeout):
            async with make_safe_async_client(
                follow_redirects=False,
                timeout=self._timeout_sec,
            ) as client:
                current_url = defuddle_url
                for _ in range(5):
                    safe, reason = is_url_safe(current_url)
                    if not safe:
                        raise ValueError(f"SSRF blocked redirect target: {reason}")
                    resp = await client.get(current_url, headers=headers)
                    if resp.status_code in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("location")
                        if not location:
                            resp.raise_for_status()
                        current_url = urljoin(current_url, location)
                        continue
                    resp.raise_for_status()
                    return resp.text
                raise ValueError("Too many redirects")

    async def aclose(self) -> None:
        pass  # No persistent resources


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    """Split YAML frontmatter from Markdown body.

    Returns (metadata_dict, markdown_body). If no valid frontmatter,
    returns ({}, raw).
    """
    lines = raw.split("\n")
    if not (lines and lines[0].strip() == _FRONTMATTER_MARKER):
        return {}, raw

    closing_idx: int | None = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FRONTMATTER_MARKER:
            closing_idx = i
            break

    if closing_idx is None:
        return {}, raw

    yaml_block = "\n".join(lines[1:closing_idx])
    markdown_body = "\n".join(lines[closing_idx + 1 :])
    return _parse_yaml_safe(yaml_block), markdown_body


def _parse_yaml_safe(yaml_text: str) -> dict[str, Any]:
    """Parse YAML, returning empty dict on any error."""
    if not yaml_text.strip():
        return {}
    try:
        import importlib

        yaml = importlib.import_module("yaml")
        result = yaml.safe_load(yaml_text)
        return result if isinstance(result, dict) else {}
    except Exception:
        logger.debug("defuddle_yaml_parse_failed", extra={"yaml_snippet": yaml_text[:100]})
        return {}
