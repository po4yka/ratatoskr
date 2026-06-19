"""Notion export adapter."""

from __future__ import annotations

from typing import Any

import httpx

from app.adapters.export.base import ExportPayload, ExportResult

_NOTION_API_URL = "https://api.notion.com/v1/pages"
_NOTION_VERSION = "2022-06-28"


class NotionExportAdapter:
    def __init__(
        self,
        *,
        token: str,
        database_id: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not database_id:
            msg = "Notion export requires config.database_id"
            raise ValueError(msg)
        self._token = token
        self._database_id = database_id
        self._client = client

    async def export(self, payload: ExportPayload) -> ExportResult:
        properties: dict[str, Any] = {
            "Name": {"title": [{"text": {"content": payload.title[:2000]}}]},
            "Summary": {"rich_text": [{"text": {"content": payload.tldr[:2000]}}]},
        }
        if payload.url:
            properties["URL"] = {"url": payload.url}
        body: dict[str, Any] = {
            "parent": {"database_id": self._database_id},
            "properties": properties,
            "children": _children(payload),
        }
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": _NOTION_VERSION,
            "Content-Type": "application/json",
        }
        if self._client is not None:
            response = await self._client.post(_NOTION_API_URL, json=body, headers=headers)
        else:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(_NOTION_API_URL, json=body, headers=headers)
        return ExportResult(
            success=response.is_success,
            response_status=response.status_code,
            response_body=response.text[:2000],
            error=None if response.is_success else response.text[:500],
        )


def _children(payload: ExportPayload) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if payload.summary_250:
        blocks.append(_paragraph(payload.summary_250))
    if payload.highlights:
        blocks.append(_heading("Highlights"))
        blocks.extend(_bulleted(item) for item in payload.highlights[:20])
    if payload.topic_tags:
        blocks.append(_paragraph("Tags: " + ", ".join(payload.topic_tags)))
    return blocks[:100]


def _paragraph(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
    }


def _heading(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
    }


def _bulleted(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text[:2000]}}]},
    }
