from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlparse, urlunparse

from app.core.logging_utils import get_logger, redact_url_for_logging
from app.core.urls.twitter import canonicalize_twitter_url
from app.core.urls.validation import _ALLOWED_SCHEMES, _DANGEROUS_SCHEMES, validate_url_input

logger = get_logger(__name__)

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
}


def extract_domain(url: str | None) -> str | None:
    """Extract normalized domain from URL (lowercase, without ``www.``)."""
    if not url:
        return None
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path.split("/")[0]
        if domain.startswith("www."):
            domain = domain[4:]
        return domain.lower() if domain else None
    except Exception:
        return None


def normalize_url(url: str) -> str:
    """Normalize a URL for deduplication as per SPEC.md."""
    validate_url_input(url)

    if "://" not in url:
        url = f"http://{url}"

    try:
        parsed = urlparse(url)

        if not parsed.netloc:
            msg = "Invalid URL: missing hostname"
            raise ValueError(msg)

        if parsed.scheme:
            scheme_lower = parsed.scheme.lower()
            if scheme_lower in _DANGEROUS_SCHEMES:
                msg = (
                    f"URL scheme '{parsed.scheme}' is not allowed. "
                    "Only http and https are supported."
                )
                raise ValueError(msg)
            if scheme_lower not in _ALLOWED_SCHEMES:
                msg = f"Unsupported URL scheme: {parsed.scheme}. Only http and https are allowed."
                raise ValueError(msg)
            scheme = scheme_lower
        else:
            scheme = "http"

        if any(char in parsed.netloc for char in ["@", "<", ">", '"', "'"]):
            msg = "URL hostname contains suspicious characters"
            raise ValueError(msg)

        netloc = parsed.netloc.lower()
        path = parsed.path or "/"

        try:
            # Fixed-point unquote: iterate until stable (cap at 5 to prevent pathological inputs)
            prev = None
            for _ in range(5):
                decoded_path = unquote(path)
                if decoded_path == prev:
                    break
                prev = decoded_path
                path = decoded_path
            path = quote(path, safe="/@:")
        except Exception as exc:
            logger.warning(
                "path_encoding_normalization_failed",
                extra={"path": path[:100], "error": str(exc)},
            )

        # Collapse consecutive slashes (e.g. /a//b/ -> /a/b)
        path = re.sub(r"/{2,}", "/", path)

        if path.endswith("/") and path != "/":
            path = path.rstrip("/")

        query_pairs = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in TRACKING_PARAMS
        ]
        query_pairs.sort(key=lambda item: (item[0], item[1]))
        query = urlencode(query_pairs)

        normalized = urlunparse((scheme, netloc, path, "", query, ""))
        logger.debug(
            "normalize_url",
            extra={
                "url": redact_url_for_logging(url),
                "normalized": redact_url_for_logging(normalized),
            },
        )
        return normalized
    except Exception as exc:
        logger.exception(
            "url_normalization_failed",
            extra={"url": redact_url_for_logging(url), "error": str(exc)},
        )
        msg = f"URL normalization failed: {exc}"
        raise ValueError(msg) from exc


def url_hash_sha256(normalized_url: str) -> str:
    """Generate SHA256 hash of normalized URL."""
    if not normalized_url or not isinstance(normalized_url, str):
        msg = "Normalized URL is required"
        raise ValueError(msg)
    if not normalized_url.strip():
        msg = "Normalized URL cannot be whitespace-only"
        raise ValueError(msg)
    if len(normalized_url) > 2048:
        msg = "Normalized URL too long"
        raise ValueError(msg)

    try:
        digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
        if len(digest) != 64:
            msg = f"Generated hash has invalid length: {len(digest)} (expected 64)"
            raise ValueError(msg)
        if not all(char in "0123456789abcdef" for char in digest):
            msg = "Generated hash contains non-hexadecimal characters"
            raise ValueError(msg)

        logger.debug("url_hash", extra={"normalized": normalized_url[:100], "sha256": digest})
        return digest
    except ValueError:
        raise
    except Exception as exc:
        logger.exception("url_hash_failed", extra={"error": str(exc)})
        msg = f"URL hashing failed: {exc}"
        raise ValueError(msg) from exc


def compute_dedupe_hash(url: str) -> str:
    """Compute deduplication hash for a URL."""
    normalized = normalize_url(url)
    twitter_canonical = canonicalize_twitter_url(normalized)
    return url_hash_sha256(twitter_canonical or normalized)


__all__ = [
    "TRACKING_PARAMS",
    "compute_dedupe_hash",
    "extract_domain",
    "normalize_url",
    "url_hash_sha256",
]
