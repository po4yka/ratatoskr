"""Bot command advertisement must not leak admin/debug commands to all users.

The bot registers its command menu at startup. Admin/debug commands
(``ADMIN_COMMAND_NAMES``) must be advertised only in each owner's own private
chat (per-peer scope), never via the default / all-private-chats scopes that
every user sees. These tests exercise ``_setup_bot_commands`` against a fake
client, so they need no Telethon and no network.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.telegram.telegram_client import ADMIN_COMMAND_NAMES, TelegramClient

pytestmark = pytest.mark.no_network


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def set_bot_commands(
        self,
        commands: list[Any],
        *,
        scope: Any = None,
        language_code: str | None = None,
        peer: int | None = None,
    ) -> None:
        self.calls.append(
            {
                "names": [c.command for c in commands],
                "scope": scope,
                "language_code": language_code,
                "peer": peer,
            }
        )

    async def set_bot_description(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def set_bot_short_description(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def set_chat_menu_button(self, *args: Any, **kwargs: Any) -> None:
        return None


class _TelegramCfgStub:
    def __init__(self, allowed_user_ids: tuple[int, ...]) -> None:
        self.allowed_user_ids = allowed_user_ids
        self.api_base_url = None


class _CfgStub:
    def __init__(self, allowed_user_ids: tuple[int, ...]) -> None:
        self.telegram = _TelegramCfgStub(allowed_user_ids)


def _make_client(allowed_user_ids: tuple[int, ...]) -> TelegramClient:
    # Bypass __init__ so no real Telethon client is constructed.
    tc = TelegramClient.__new__(TelegramClient)
    tc.cfg = _CfgStub(allowed_user_ids)  # type: ignore[assignment]
    tc.client = _RecordingClient()  # type: ignore[assignment]
    tc.topic_manager = None
    return tc


def test_bot_session_uses_configured_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, Any] = {}

    class _ClientStub:
        def __init__(self, **kwargs: Any) -> None:
            recorded.update(kwargs)

    monkeypatch.setattr(
        "app.adapters.telegram.telegram_client.TelethonBotClient",
        _ClientStub,
    )
    cfg = SimpleNamespace(
        telegram=SimpleNamespace(
            api_id=1,
            api_hash="hash",
            bot_token="1:token",
            session_dir="/data",
        )
    )

    TelegramClient(cfg)

    assert recorded["name"] == "ratatoskr_bot"
    assert recorded["session_dir"] == "/data"


@pytest.mark.asyncio
async def test_admin_commands_never_advertised_to_public_scopes() -> None:
    tc = _make_client((111, 222))
    await tc._setup_bot_commands()

    public_calls = [c for c in tc.client.calls if c["peer"] is None]  # type: ignore[attr-defined]
    assert public_calls, "expected default + all-private-chats registrations"
    for call in public_calls:
        leaked = ADMIN_COMMAND_NAMES.intersection(call["names"])
        assert not leaked, f"admin commands leaked to a public scope: {sorted(leaked)}"


@pytest.mark.asyncio
async def test_owner_scope_includes_full_command_set() -> None:
    tc = _make_client((111, 222))
    await tc._setup_bot_commands()

    owner_calls = [c for c in tc.client.calls if c["peer"] is not None]  # type: ignore[attr-defined]
    assert {c["peer"] for c in owner_calls} == {111, 222}
    for call in owner_calls:
        assert ADMIN_COMMAND_NAMES.issubset(call["names"]), (
            "each owner scope must advertise every admin command"
        )


@pytest.mark.asyncio
async def test_no_owners_means_no_peer_scoped_calls() -> None:
    tc = _make_client(())
    await tc._setup_bot_commands()

    assert all(c["peer"] is None for c in tc.client.calls)  # type: ignore[attr-defined]
    # Public commands are still advertised.
    assert any(c["peer"] is None for c in tc.client.calls)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_one_owner_resolution_failure_does_not_block_others() -> None:
    tc = _make_client((111, 222))
    recording = tc.client  # type: ignore[assignment]
    original = recording.set_bot_commands

    async def flaky(
        commands: list[Any],
        *,
        scope: Any = None,
        language_code: str | None = None,
        peer: int | None = None,
    ) -> None:
        if peer == 111:
            raise RuntimeError("cannot resolve owner peer")
        await original(commands, scope=scope, language_code=language_code, peer=peer)

    recording.set_bot_commands = flaky  # type: ignore[assignment,method-assign]

    await tc._setup_bot_commands()

    owner_peers = {c["peer"] for c in recording.calls if c["peer"] is not None}
    assert owner_peers == {222}, "a failing owner must not suppress the others"
