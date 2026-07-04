"""Resolve Twitter/X article links with redirect and canonical URL hints."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin, urlparse, urlunparse

from app.core.urls.twitter import canonicalize_twitter_url, extract_twitter_article_id
from app.security.ssrf import is_url_safe_async, make_safe_async_client

ResolutionReason = Literal[
    "path_match",
    "redirect_match",
    "canonical_match",
    "not_article",
    "resolve_failed",
]

_TWITTER_HOSTS = {
    "x.com",
    "twitter.com",
    "www.x.com",
    "www.twitter.com",
    "mobile.x.com",
    "mobile.twitter.com",
}
_RESOLVABLE_HOSTS = _TWITTER_HOSTS | {"t.co"}
_CANONICAL_LINK_RE = re.compile(
    r"""<link[^>]+rel=["']canonical["'][^>]+href=["'](?P<href>[^"']+)["']""",
    flags=re.IGNORECASE,
)
_OG_URL_RE = re.compile(
    r"""<meta[^>]+property=["']og:url["'][^>]+content=["'](?P<href>[^"']+)["']""",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True)
class TwitterArticleLinkResolution:
    """Resolution result for an incoming URL potentially pointing to an X Article."""

    input_url: str
    resolved_url: str | None
    canonical_url: str | None
    article_id: str | None
    is_article: bool
    reason: ResolutionReason


def _normalize_output_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunparse(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", parsed.query, "")
    )


def _extract_canonical_hint(base_url: str, html_text: str) -> str | None:
    if not html_text:
        return None
    truncated = html_text[:100_000]
    for pattern in (_CANONICAL_LINK_RE, _OG_URL_RE):
        match = pattern.search(truncated)
        if not match:
            continue
        href = (match.group("href") or "").strip()
        if not href:
            continue
        return _normalize_output_url(urljoin(base_url, href))
    return None


def _host_for_resolution(url: str) -> str:
    candidate = url.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    return (urlparse(candidate).hostname or "").lower()


def _build_result(
    *,
    input_url: str,
    reason: ResolutionReason,
    resolved_url: str | None = None,
    canonical_url: str | None = None,
    article_id: str | None = None,
) -> TwitterArticleLinkResolution:
    return TwitterArticleLinkResolution(
        input_url=input_url,
        resolved_url=resolved_url,
        canonical_url=canonical_url,
        article_id=article_id,
        is_article=article_id is not None,
        reason=reason,
    )


async def resolve_twitter_article_link(
    url: str,
    timeout_s: float = 5.0,
) -> TwitterArticleLinkResolution:
    """Resolve whether a URL points to an X Article.

    Resolution strategy:
    1. Direct path match (no network)
    2. Redirect follow via HEAD
    3. Fallback GET for canonical meta hints when HEAD is unsupported/insufficient
    """
    normalized_input = _normalize_output_url(url) or url

    direct_article_id = extract_twitter_article_id(normalized_input)
    if direct_article_id:
        direct_canonical = canonicalize_twitter_url(normalized_input)
        return _build_result(
            input_url=url,
            resolved_url=normalized_input,
            canonical_url=direct_canonical,
            article_id=direct_article_id,
            reason="path_match",
        )

    host = _host_for_resolution(normalized_input)
    if host not in _RESOLVABLE_HOSTS:
        return _build_result(input_url=url, reason="not_article")

    # Preflight SSRF check before making any network request.
    safe, _ = await is_url_safe_async(normalized_input)
    if not safe:
        return _build_result(input_url=url, reason="resolve_failed")

    try:
        resolved_url: str | None = None
        canonical_hint: str | None = None

        async with make_safe_async_client(follow_redirects=False, timeout=timeout_s) as client:
            # HEAD with manual redirect following (5-hop cap, per-hop SSRF check).
            current_url = normalized_input
            head_resp = None
            for _ in range(5):
                safe, _ = await is_url_safe_async(current_url)
                if not safe:
                    return _build_result(input_url=url, reason="resolve_failed")
                head_resp = await client.head(current_url)
                if head_resp.status_code in {301, 302, 303, 307, 308}:
                    location = head_resp.headers.get("location")
                    if not location:
                        break
                    current_url = urljoin(current_url, location)
                    continue
                break

            if head_resp is None:
                return _build_result(input_url=url, reason="resolve_failed")

            resolved_url = _normalize_output_url(current_url)
            needs_get = head_resp.status_code in {403, 405, 406, 415, 429, 500, 501}

            if needs_get:
                current_url = normalized_input
                for _ in range(5):
                    safe, _ = await is_url_safe_async(current_url)
                    if not safe:
                        break
                    get_resp = await client.get(current_url)
                    if get_resp.status_code in {301, 302, 303, 307, 308}:
                        location = get_resp.headers.get("location")
                        if not location:
                            break
                        current_url = urljoin(current_url, location)
                        continue
                    resolved_url = _normalize_output_url(current_url)
                    content_type = (get_resp.headers.get("content-type") or "").lower()
                    if "html" in content_type:
                        canonical_hint = _extract_canonical_hint(current_url, get_resp.text)
                    break

        redirect_article_id = extract_twitter_article_id(resolved_url or "")
        if redirect_article_id:
            return _build_result(
                input_url=url,
                resolved_url=resolved_url,
                canonical_url=canonicalize_twitter_url(resolved_url or normalized_input),
                article_id=redirect_article_id,
                reason="redirect_match",
            )

        canonical_article_id = extract_twitter_article_id(canonical_hint or "")
        if canonical_article_id:
            return _build_result(
                input_url=url,
                resolved_url=resolved_url,
                canonical_url=canonicalize_twitter_url(canonical_hint or ""),
                article_id=canonical_article_id,
                reason="canonical_match",
            )

        return _build_result(
            input_url=url,
            resolved_url=resolved_url,
            canonical_url=canonical_hint,
            reason="not_article",
        )
    except Exception:
        return _build_result(input_url=url, reason="resolve_failed")
