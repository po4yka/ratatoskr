"""
Proxy endpoints for external resources.
"""

from typing import Any

import httpx
from fastapi import APIRouter, Depends, Query
from starlette.responses import Response

from app.api.exceptions import (
    AuthorizationError,
    ExternalAPIError,
    ProcessingError,
    ResourceNotFoundError,
    ValidationError,
)
from app.api.routers.auth import get_current_user
from app.core.logging_utils import get_logger, log_exception
from app.security.ssrf import is_url_safe_async, make_safe_async_client

logger = get_logger(__name__)
router = APIRouter()

_MAX_PROXY_RESPONSE_BYTES = 10 * 1024 * 1024  # 10 MB


async def _read_limited_content(response: httpx.Response, max_bytes: int) -> bytes:
    """Read full response content with a strict cumulative size limit."""
    total = 0
    chunks: list[bytes] = []
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > max_bytes:
            logger.warning(
                "proxy_response_size_exceeded",
                extra={"total_bytes": total, "max_bytes": max_bytes},
            )
            raise ValidationError(f"Image too large (max {max_bytes // (1024 * 1024)} MB)")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/image", response_class=Response, response_model=None)
async def proxy_image(
    url: str = Query(..., description="URL of the image to proxy"),
    _user: dict[str, Any] = Depends(get_current_user),
) -> Response:
    """
    Proxy an image from a remote URL.

    This endpoint fetches an image from the specified URL and streams it back to the client.
    It helps bypass CORs issues, mixed content warnings (loading HTTP images on HTTPS sites),
    and potential hotlink protection.
    """
    if not url.startswith(("http://", "https://")):
        raise ValidationError("Invalid URL scheme")

    try:
        # Use a real browser User-Agent to avoid getting blocked by some servers
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
        }

        async with make_safe_async_client(follow_redirects=False, timeout=10.0) as client:
            max_redirects = 5
            current_url = url
            for _ in range(max_redirects + 1):
                # SSRF protection: block requests to internal/private networks
                safe, reason = await is_url_safe_async(current_url)
                if not safe:
                    logger.warning(
                        "proxy_blocked_ssrf", extra={"url": current_url, "reason": reason}
                    )
                    raise AuthorizationError("URL resolves to blocked address")

                req = client.build_request("GET", current_url, headers=headers)
                resp = await client.send(req, stream=True)

                if resp.status_code in {301, 302, 303, 307, 308}:
                    location = resp.headers.get("location")
                    await resp.aclose()
                    if not location:
                        raise ExternalAPIError("upstream", "Redirect missing location")
                    next_url = str(httpx.URL(current_url).join(location))
                    if not next_url.startswith(("http://", "https://")):
                        raise ValidationError("Invalid redirect URL scheme")
                    current_url = next_url
                    continue
                break
            else:
                raise ExternalAPIError("upstream", "Too many redirects")

            if resp.status_code >= 400:
                await resp.aclose()
                logger.warning(
                    "proxy_fetch_failed",
                    extra={"url": current_url, "status_code": resp.status_code},
                )
                raise ResourceNotFoundError("Image", current_url)

            content_type = resp.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                await resp.aclose()
                logger.warning(
                    "proxy_non_image_content",
                    extra={"url": current_url, "content_type": content_type},
                )
                raise ValidationError("URL does not point to an image")

            content_length_header = resp.headers.get("content-length")
            if content_length_header and content_length_header.isdigit():
                if int(content_length_header) > _MAX_PROXY_RESPONSE_BYTES:
                    await resp.aclose()
                    logger.warning(
                        "proxy_response_declared_size_exceeded",
                        extra={
                            "declared_size": int(content_length_header),
                            "max_bytes": _MAX_PROXY_RESPONSE_BYTES,
                        },
                    )
                    raise ValidationError(
                        f"Image too large (max {_MAX_PROXY_RESPONSE_BYTES // (1024 * 1024)} MB)"
                    )

            try:
                image_bytes = await _read_limited_content(resp, _MAX_PROXY_RESPONSE_BYTES)
            finally:
                await resp.aclose()

            return Response(
                content=image_bytes,
                media_type=content_type,
                headers={
                    "Cache-Control": "public, max-age=86400",  # Cache for 1 day
                },
            )

    except httpx.RequestError as e:
        log_exception(logger, "proxy_request_error", e, url=url)
        raise ExternalAPIError("upstream", "Failed to fetch upstream image") from e
    except (ValidationError, AuthorizationError, ExternalAPIError, ResourceNotFoundError):
        raise
    except Exception as e:
        log_exception(logger, "proxy_unexpected_error", e, url=url)
        raise ProcessingError("Internal proxy error") from e
