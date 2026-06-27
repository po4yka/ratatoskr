"""Tests for the ChatGPT backup client (against a fake fetcher)."""

from __future__ import annotations

import base64
import datetime as dt
import json

import pytest

from app.adapters.ai_backup.chatgpt_client import ChatGptClient
from app.adapters.ai_backup.disk_writer import AiBackupDiskWriter
from app.adapters.ai_backup.errors import AiBackupAuthExpiredError
from app.adapters.content.browser_auth.authenticated_context import FetchResponse

_DATE = dt.date(2026, 6, 27)


def _json(obj: object, status: int = 200) -> FetchResponse:
    return FetchResponse(status=status, body_bytes=json.dumps(obj).encode("utf-8"))


def _jwt(claims: dict) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"header.{payload}.sig"


def _writer(tmp_path) -> AiBackupDiskWriter:
    return AiBackupDiskWriter(tmp_path, "chatgpt", _DATE, "c")


async def test_exchange_extracts_team_account_id(tmp_path, fake_fetcher) -> None:
    token = _jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_plan_type": "team",
                "chatgpt_account_id": "acct-1",
            }
        }
    )
    fetcher = fake_fetcher(lambda url: _json({"accessToken": token}))
    client = ChatGptClient(fetcher, _writer(tmp_path))
    await client.exchange_session_cookie()
    assert client._account_id == "acct-1"
    assert client._make_headers()["chatgpt-account-id"] == "acct-1"


async def test_exchange_personal_plan_has_no_account_id(tmp_path, fake_fetcher) -> None:
    token = _jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}})
    fetcher = fake_fetcher(lambda url: _json({"accessToken": token}))
    client = ChatGptClient(fetcher, _writer(tmp_path))
    await client.exchange_session_cookie()
    assert client._account_id is None
    assert "chatgpt-account-id" not in client._make_headers()


async def test_exchange_missing_token_raises(tmp_path, fake_fetcher) -> None:
    fetcher = fake_fetcher(lambda url: _json({}))
    client = ChatGptClient(fetcher, _writer(tmp_path))
    with pytest.raises(AiBackupAuthExpiredError):
        await client.exchange_session_cookie()


async def test_collect_happy_path_with_file(tmp_path, fake_fetcher) -> None:
    token = _jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}})

    def handler(url: str) -> object:
        if "/api/auth/session" in url:
            return _json({"accessToken": token})
        if "/backend-api/conversations?" in url:
            if "is_archived=true" in url:
                return _json({"items": []})
            return _json({"items": [{"id": "c1", "update_time": 1000.0}]})
        if "/backend-api/conversation/c1" in url:
            return _json(
                {
                    "mapping": {
                        "n1": {
                            "message": {
                                "content": {
                                    "content_type": "multimodal_text",
                                    "parts": [{"asset_pointer": "sediment://file_abc"}],
                                }
                            }
                        }
                    }
                }
            )
        if "/backend-api/gizmos/snorlax/sidebar" in url:
            return _json({"items": [], "cursor": None})
        if "/backend-api/files/download/file_abc" in url:
            return _json(
                {
                    "status": "success",
                    "download_url": "https://files.oaiusercontent.com/x",
                    "file_name": "a.png",
                }
            )
        if "files.oaiusercontent.com" in url:
            return FetchResponse(status=200, body_bytes=b"PNGDATA")
        return None

    writer = _writer(tmp_path)
    client = ChatGptClient(fake_fetcher(handler), writer)
    counts = await client.collect()
    assert counts["conversations"] == 1
    assert counts["files"] == 1
    assert (writer.run_dir / "conversations" / "c1.json").exists()
    assert (writer.run_dir / "files" / "file_abc__a.png").read_bytes() == b"PNGDATA"


async def test_collect_incremental_skips_unchanged(tmp_path, fake_fetcher) -> None:
    token = _jwt({"https://api.openai.com/auth": {"chatgpt_plan_type": "pro"}})
    since = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)

    def handler(url: str) -> object:
        if "/api/auth/session" in url:
            return _json({"accessToken": token})
        if "/backend-api/conversations?" in url:
            if "is_archived=true" in url:
                return _json({"items": []})
            return _json({"items": [{"id": "c1", "update_time": 1000.0}]})  # 1970, before since
        if "/backend-api/gizmos/snorlax/sidebar" in url:
            return _json({"items": [], "cursor": None})
        if "/backend-api/conversation/" in url:
            raise AssertionError("detail must not be fetched for a skipped conversation")
        return None

    client = ChatGptClient(fake_fetcher(handler), _writer(tmp_path), last_backed_up_at=since)
    counts = await client.collect()
    assert counts["conversations"] == 0
    assert client.skipped == 1


async def test_401_raises_auth_expired(tmp_path, fake_fetcher) -> None:
    fetcher = fake_fetcher(lambda url: FetchResponse(status=401, body_bytes=b"{}"))
    client = ChatGptClient(fetcher, _writer(tmp_path))
    with pytest.raises(AiBackupAuthExpiredError):
        await client.collect()
