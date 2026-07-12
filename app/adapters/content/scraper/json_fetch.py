"""Size-capped JSON fetch shared by the Reddit and Hacker News providers.

Both providers read a public JSON API (Reddit's comments endpoint, HN's Algolia
item endpoint). ``httpx`` imposes no response-body ceiling, so ``response.json()``
buffers the entire body into memory before it can be inspected -- a hostile or
misbehaving upstream can force unbounded allocation. This helper streams the
body, rejecting it via the declared ``Content-Length`` (cheap early-out) and via
a cumulative byte count over ``aiter_bytes()`` (authoritative, since
``Content-Length`` can be absent or wrong), before any JSON parsing. It mirrors
the cap that ``DefuddleProvider``/``Crawl4AIProvider`` apply to their bodies.
"""

from __future__ import annotations

import json
from typing import Any


def _reject_oversized_content_length(response: Any, max_bytes: int) -> None:
    content_length = response.headers.get("content-length")
    if not content_length:
        return
    try:
        declared = int(content_length)
    except ValueError:
        return
    if declared > max_bytes:
        raise ValueError(f"JSON response exceeds {max_bytes} byte limit")


async def read_json_capped(
    client: Any,
    endpoint: str,
    *,
    headers: dict[str, str],
    max_bytes: int,
) -> tuple[Any, int]:
    """GET ``endpoint`` and parse JSON, refusing bodies larger than ``max_bytes``.

    Streams the response so an oversized body is rejected without being fully
    buffered. Returns ``(parsed_json, status_code)``. Raises ``ValueError`` when
    the body exceeds the cap and propagates ``httpx`` errors (including
    ``raise_for_status``) to the caller.
    """
    async with client.stream("GET", endpoint, headers=headers) as response:
        response.raise_for_status()
        _reject_oversized_content_length(response, max_bytes)

        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"JSON response exceeds {max_bytes} byte limit")
            chunks.append(chunk)
        status_code = response.status_code

    return json.loads(b"".join(chunks)), status_code
