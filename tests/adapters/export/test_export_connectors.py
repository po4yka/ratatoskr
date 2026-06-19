"""Tests for outbound export connector adapters."""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters.export.base import ExportPayload, payload_from_summary_context, render_markdown
from app.adapters.export.notion_export import NotionExportAdapter
from app.adapters.export.obsidian_export import ObsidianExportAdapter
from app.adapters.export.readwise_export import ReadwiseExportAdapter


def _payload() -> ExportPayload:
    return ExportPayload(
        summary_id=42,
        request_id=7,
        url="https://example.com/article",
        title="Example Article",
        tldr="Short version.",
        summary_250="A concise summary.",
        topic_tags=["#ai", "#tools"],
        highlights=["First idea", "Second idea"],
    )


def test_payload_from_summary_context_extracts_export_fields() -> None:
    payload = payload_from_summary_context(
        {
            "summary": {
                "id": 42,
                "request_id": 7,
                "json_payload": {
                    "tldr": "TLDR",
                    "summary_250": "Summary",
                    "topic_tags": ["#ai"],
                    "key_ideas": ["Idea"],
                    "metadata": {"title": "Title"},
                },
            },
            "request": {"id": 7, "input_url": "https://example.com"},
        }
    )

    assert payload.title == "Title"
    assert payload.url == "https://example.com"
    assert payload.highlights == ["Idea"]


@pytest.mark.asyncio
async def test_notion_export_posts_page_to_database() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["version"] = request.headers["Notion-Version"]
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"id": "page-id"})

    adapter = NotionExportAdapter(
        token="secret",
        database_id="database-id",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await adapter.export(_payload())

    assert result.success is True
    assert seen["url"] == "https://api.notion.com/v1/pages"
    assert seen["auth"] == "Bearer secret"
    body = seen["json"]
    assert isinstance(body, dict)
    assert body["parent"] == {"database_id": "database-id"}
    assert body["properties"]["URL"] == {"url": "https://example.com/article"}


@pytest.mark.asyncio
async def test_readwise_export_posts_highlights() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["Authorization"]
        seen["json"] = json.loads(request.content)
        return httpx.Response(201, json={"ok": True})

    adapter = ReadwiseExportAdapter(
        token="rw-token",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )

    result = await adapter.export(_payload())

    assert result.success is True
    assert seen["auth"] == "Token rw-token"
    body = seen["json"]
    assert isinstance(body, dict)
    assert [item["text"] for item in body["highlights"]] == ["First idea", "Second idea"]
    assert body["highlights"][0]["source_url"] == "https://example.com/article"


@pytest.mark.asyncio
async def test_obsidian_export_writes_markdown(tmp_path) -> None:
    adapter = ObsidianExportAdapter(vault_path=str(tmp_path), folder="Ratatoskr")

    result = await adapter.export(_payload())

    assert result.success is True
    path = tmp_path / "Ratatoskr" / "Example-Article-42.md"
    assert path.exists()
    assert path.read_text(encoding="utf-8") == render_markdown(_payload())
