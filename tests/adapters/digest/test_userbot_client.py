from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.digest import userbot_client as userbot_module
from app.adapters.digest.userbot_client import UserbotClient, _telethon_media_type
from app.core.time_utils import UTC


class _FakeTelethonUserClient:
    started: list["_FakeTelethonUserClient"] = []

    def __init__(self, *, session_path: str, api_id: int, api_hash: str) -> None:
        self.session_path = session_path
        self.api_id = api_id
        self.api_hash = api_hash
        self.is_connected = True
        self.disconnected = False
        _FakeTelethonUserClient.started.append(self)

    async def start(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.disconnected = True
        self.is_connected = False

    async def get_chat_history(self, channel_username: str) -> Any:
        for message in self.messages:
            yield message

    async def get_chat(self, username: str) -> object:
        if username == "missing":
            raise RuntimeError("missing")
        return SimpleNamespace(
            username="resolved",
            title="Title",
            about="About",
            participants_count=123,
        )


def _cfg() -> SimpleNamespace:
    return SimpleNamespace(
        digest=SimpleNamespace(session_name="digest_user"),
        telegram=SimpleNamespace(api_id=123, api_hash="hash"),
    )


def test_media_type_mapping() -> None:
    class PhotoMedia:
        pass

    class DocumentMedia:
        pass

    class OtherMedia:
        pass

    assert _telethon_media_type(None) is None
    assert _telethon_media_type(PhotoMedia()) == "photo"
    assert _telethon_media_type(DocumentMedia()) == "document"
    assert _telethon_media_type(OtherMedia()) == "media"


@pytest.mark.asyncio
async def test_start_requires_session_file_and_starts_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(userbot_module, "TelethonUserClient", _FakeTelethonUserClient)
    subject = UserbotClient(_cfg(), tmp_path)  # type: ignore[arg-type]

    with pytest.raises(FileNotFoundError):
        await subject.start()

    (tmp_path / "digest_user.session").write_text("", encoding="utf-8")
    await subject.start()

    assert subject.is_connected
    assert _FakeTelethonUserClient.started[-1].session_path == str(tmp_path / "digest_user")

    client = subject._client
    await subject.stop()
    assert client is not None
    assert client.disconnected
    assert not subject.is_connected


@pytest.mark.asyncio
async def test_fetch_channel_posts_filters_by_date_and_length() -> None:
    subject = UserbotClient(_cfg(), Path("/tmp"))  # type: ignore[arg-type]
    client = _FakeTelethonUserClient(session_path="s", api_id=1, api_hash="h")
    now = datetime.now(UTC)
    client.messages = [
        SimpleNamespace(
            id=1,
            date=now,
            message="long enough text",
            views=10,
            forwards=2,
            media=SimpleNamespace(__class__=type("PhotoMedia", (), {})),
        ),
        SimpleNamespace(id=2, date=now, message="short", views=None, forwards=None, media=None),
        SimpleNamespace(id=3, date=(now - timedelta(hours=30)).replace(tzinfo=None), text="old text", media=None),
    ]
    subject._client = client

    posts = await subject.fetch_channel_posts("channel", hours_lookback=24, min_length=10)

    assert len(posts) == 1
    assert posts[0]["message_id"] == 1
    assert posts[0]["url"] == "https://t.me/channel/1"
    assert posts[0]["views"] == 10


@pytest.mark.asyncio
async def test_fetch_and_resolve_require_started_client() -> None:
    subject = UserbotClient(_cfg(), Path("/tmp"))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="not started"):
        await subject.fetch_channel_posts("channel")
    with pytest.raises(RuntimeError, match="not started"):
        await subject.resolve_channel("channel")


@pytest.mark.asyncio
async def test_resolve_channel_maps_metadata_and_errors() -> None:
    subject = UserbotClient(_cfg(), Path("/tmp"))  # type: ignore[arg-type]
    subject._client = _FakeTelethonUserClient(session_path="s", api_id=1, api_hash="h")

    assert await subject.resolve_channel("channel") == {
        "username": "resolved",
        "title": "Title",
        "description": "About",
        "member_count": 123,
    }

    with pytest.raises(ValueError, match="Could not resolve channel"):
        await subject.resolve_channel("missing")
