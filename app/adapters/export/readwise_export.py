"""Readwise export adapter."""

from __future__ import annotations

import httpx

from app.adapters.export.base import ExportPayload, ExportResult

_READWISE_HIGHLIGHTS_URL = "https://readwise.io/api/v2/highlights/"


class ReadwiseExportAdapter:
    def __init__(self, *, token: str, client: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._client = client

    async def export(self, payload: ExportPayload) -> ExportResult:
        highlights = payload.highlights or [payload.tldr or payload.summary_250]
        body = {
            "highlights": [
                {
                    "text": text[:8192],
                    "title": payload.title[:512],
                    "source_url": payload.url,
                    "category": "articles",
                    "note": payload.summary_250[:8192],
                    "tags": [tag.lstrip("#") for tag in payload.topic_tags[:20]],
                }
                for text in highlights
                if text
            ]
        }
        headers = {"Authorization": f"Token {self._token}", "Content-Type": "application/json"}
        if self._client is not None:
            response = await self._client.post(_READWISE_HIGHLIGHTS_URL, json=body, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(_READWISE_HIGHLIGHTS_URL, json=body, headers=headers)
        return ExportResult(
            success=response.is_success,
            response_status=response.status_code,
            response_body=response.text[:2000],
            error=None if response.is_success else response.text[:500],
        )
