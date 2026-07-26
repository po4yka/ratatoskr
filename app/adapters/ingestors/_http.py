"""Size-capped JSON fetch shared by the public source ingestors.

The Reddit and Hacker News ingestors poll public JSON APIs. ``httpx`` imposes no
response-body ceiling, so ``response.json()`` buffers the entire body into memory
before it can be inspected -- a hostile or misbehaving upstream can force
unbounded allocation on the ingestion path. This helper streams the body,
rejecting it via the declared ``Content-Length`` (cheap early-out) and via a
cumulative byte count over ``aiter_bytes()`` (authoritative, since
``Content-Length`` can be absent or wrong) before any JSON parsing. It mirrors
the cap the content-scraper Reddit/HN providers apply in
``app/adapters/content/scraper/json_fetch.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.application.ports.source_ingestors import TransientSourceError

if TYPE_CHECKING:
    from collections.abc import Callable

DEFAULT_MAX_RESPONSE_MB = 10


def _reject_oversized_content_length(response: Any, max_bytes: int, *, provider: str) -> None:
    content_length = response.headers.get("content-length")
    if not content_length:
        return
    try:
        declared = int(content_length)
    except ValueError:
        return
    if declared > max_bytes:
        raise TransientSourceError(f"{provider} response exceeds {max_bytes} byte cap")


async def fetch_json_capped(
    client: Any,
    url: str,
    *,
    max_bytes: int,
    provider: str,
    headers: dict[str, str] | None = None,
    check_status: Callable[[Any], None] | None = None,
) -> Any:
    """GET ``url`` and parse JSON, refusing bodies larger than ``max_bytes``.

    Streams the response so an oversized body is rejected without being fully
    buffered (unlike ``response.json()``). ``check_status``, when supplied, runs
    against the response before the body is read, so callers keep mapping status
    codes to their own exceptions (rate-limit / auth / transient) exactly as
    before. Raises ``TransientSourceError`` when the cap is exceeded.
    """
    async with client.stream("GET", url, headers=headers) as response:
        if check_status is not None:
            check_status(response)
        _reject_oversized_content_length(response, max_bytes, provider=provider)

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise TransientSourceError(f"{provider} response exceeds {max_bytes} byte cap")
            chunks.append(chunk)
        body = b"".join(chunks)

    return json.loads(body)
