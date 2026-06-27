"""Tests for the Claude backup client (against a fake fetcher)."""

from __future__ import annotations

import datetime as dt
import json

import pytest

from app.adapters.ai_backup.claude_client import ClaudeClient
from app.adapters.ai_backup.disk_writer import AiBackupDiskWriter
from app.adapters.ai_backup.errors import AiBackupAuthExpiredError
from app.adapters.content.browser_auth.authenticated_context import FetchResponse

_DATE = dt.date(2026, 6, 27)


def _json(obj: object, status: int = 200) -> FetchResponse:
    return FetchResponse(status=status, body_bytes=json.dumps(obj).encode("utf-8"))


def _writer(tmp_path) -> AiBackupDiskWriter:
    return AiBackupDiskWriter(tmp_path, "claude", _DATE, "c")


def _detail_with_new_artifact() -> dict:
    return {
        "chat_messages": [
            {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "artifacts",
                        "id": "art1",
                        "display_content": {
                            "type": "code_block",
                            "language": "py",
                            "code": "print('hi')",
                        },
                    }
                ]
            }
        ]
    }


async def test_collect_happy_path(tmp_path, fake_fetcher) -> None:
    def handler(url: str) -> object:
        if url.endswith("/api/account"):
            return _json(
                {"memberships": [{"organization": {"uuid": "org1", "capabilities": ["chat"]}}]}
            )
        if url.endswith("/projects"):
            return _json([{"uuid": "p1", "name": "Proj"}])
        if url.endswith("/chat_conversations"):
            return _json([{"uuid": "u1", "updated_at": "2026-06-01T00:00:00Z"}])
        if "/chat_conversations/u1" in url:
            return _json(_detail_with_new_artifact())
        return None

    writer = _writer(tmp_path)
    client = ClaudeClient(fake_fetcher(handler), writer, incremental=False)
    counts = await client.collect()
    assert counts == {"conversations": 1, "projects": 1, "files": 0, "artifacts": 1}
    assert (writer.run_dir / "conversations" / "u1.json").exists()
    assert (writer.run_dir / "projects" / "p1" / "project.json").exists()
    assert (writer.run_dir / "artifacts" / "u1" / "art1.py").read_text() == "print('hi')"


async def test_org_fallback_to_organizations(tmp_path, fake_fetcher) -> None:
    def handler(url: str) -> object:
        if url.endswith("/api/account"):
            return _json(
                {"memberships": [{"organization": {"uuid": "x", "capabilities": ["api"]}}]}
            )
        if url.endswith("/api/organizations"):
            return _json([{"uuid": "org-fallback", "capabilities": ["chat"]}])
        if url.endswith("/projects"):
            return _json([])
        if url.endswith("/chat_conversations"):
            return _json([])
        return None

    client = ClaudeClient(fake_fetcher(handler), _writer(tmp_path))
    counts = await client.collect()
    assert client._org_id == "org-fallback"
    assert counts["conversations"] == 0


async def test_old_antartifact_format(tmp_path, fake_fetcher) -> None:
    detail = {
        "chat_messages": [
            {
                "content": [
                    {
                        "text": '<antArtifact identifier="old1" type="application/vnd.ant.code" '
                        'language="js">console.log(1)</antArtifact>'
                    }
                ]
            }
        ]
    }

    def handler(url: str) -> object:
        if url.endswith("/api/account"):
            return _json(
                {"memberships": [{"organization": {"uuid": "o", "capabilities": ["chat"]}}]}
            )
        if url.endswith("/projects"):
            return _json([])
        if url.endswith("/chat_conversations"):
            return _json([{"uuid": "u1", "updated_at": "2026-06-01T00:00:00Z"}])
        if "/chat_conversations/u1" in url:
            return _json(detail)
        return None

    writer = _writer(tmp_path)
    client = ClaudeClient(fake_fetcher(handler), writer, incremental=False)
    counts = await client.collect()
    assert counts["artifacts"] == 1
    assert (writer.run_dir / "artifacts" / "u1" / "old1.js").read_text() == "console.log(1)"


async def test_401_raises_auth_expired(tmp_path, fake_fetcher) -> None:
    def handler(url: str) -> object:
        if url.endswith("/api/account"):
            return _json(
                {"memberships": [{"organization": {"uuid": "o", "capabilities": ["chat"]}}]}
            )
        if url.endswith("/projects"):
            return _json([])
        if url.endswith("/chat_conversations"):
            return FetchResponse(status=401, body_bytes=b"{}")
        return None

    client = ClaudeClient(fake_fetcher(handler), _writer(tmp_path))
    with pytest.raises(AiBackupAuthExpiredError):
        await client.collect()


async def test_cloudflare_html_interstitial_is_auth_expired(tmp_path, fake_fetcher) -> None:
    def handler(url: str) -> object:
        if url.endswith("/api/account"):
            return FetchResponse(status=200, body_bytes=b"<!DOCTYPE html><html>cf</html>")
        return None

    client = ClaudeClient(fake_fetcher(handler), _writer(tmp_path))
    with pytest.raises(AiBackupAuthExpiredError):
        await client.collect()


async def test_incremental_skip(tmp_path, fake_fetcher) -> None:
    since = dt.datetime(2026, 6, 15, tzinfo=dt.UTC)

    def handler(url: str) -> object:
        if url.endswith("/api/account"):
            return _json(
                {"memberships": [{"organization": {"uuid": "o", "capabilities": ["chat"]}}]}
            )
        if url.endswith("/projects"):
            return _json([])
        if url.endswith("/chat_conversations"):
            return _json([{"uuid": "u1", "updated_at": "2026-06-01T00:00:00Z"}])  # before since
        if "/chat_conversations/u1" in url:
            raise AssertionError("skipped conversation must not be fetched")
        return None

    client = ClaudeClient(fake_fetcher(handler), _writer(tmp_path), last_backed_up_at=since)
    counts = await client.collect()
    assert counts["conversations"] == 0
    assert client.skipped == 1
