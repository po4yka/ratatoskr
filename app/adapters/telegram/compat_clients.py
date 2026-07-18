"""Bot + user transport clients wrapping Telethon's TelegramClient."""

from __future__ import annotations

import json
from typing import Any

from app.adapters.telegram.compat_adapters import (
    TelethonCallbackQueryAdapter,
    TelethonMessageAdapter,
    TelethonReactionAdapter,
)
from app.adapters.telegram.compat_entities import _build_typing_tl_action
from app.adapters.telegram.compat_keyboards import _filter_send_kwargs, to_telethon_buttons
from app.adapters.telegram.compat_telethon import (
    TelegramClient,
    events,
    functions,
    types,
)
from app.adapters.telegram.compat_types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    normalize_parse_mode,
)
from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger

logger = get_logger(__name__)


class TelethonBotClient:
    """Small compatibility wrapper around Telethon's bot client."""

    def __init__(
        self,
        *,
        name: str,
        api_id: int,
        api_hash: str,
        bot_token: str,
        session_dir: str | None = None,
    ) -> None:
        if TelegramClient is None:
            msg = "Telethon is not installed"
            raise RuntimeError(msg)
        session_name = name if session_dir is None else f"{session_dir.rstrip('/')}/{name}"
        self._bot_token = bot_token
        self._client = TelegramClient(session_name, api_id, api_hash)

    @property
    def raw(self) -> Any:
        return self._client

    @property
    def is_connected(self) -> bool:
        return bool(self._client.is_connected())

    async def start(self) -> None:
        await self._client.start(bot_token=self._bot_token)

    async def stop(self) -> None:
        await self._client.disconnect()

    async def __aenter__(self) -> TelethonBotClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()

    def add_message_handler(self, handler: Any) -> None:
        if events is None:
            return

        @self._client.on(events.NewMessage(incoming=True))  # type: ignore[untyped-decorator, unused-ignore]
        async def _on_message(event: Any) -> None:
            await handler(TelethonMessageAdapter(event, self))

    def add_callback_query_handler(self, handler: Any) -> None:
        if events is None:
            return

        @self._client.on(events.CallbackQuery)  # type: ignore[untyped-decorator, unused-ignore]
        async def _on_callback(event: Any) -> None:
            await handler(TelethonCallbackQueryAdapter(event, self))

    def add_reaction_handler(self, handler: Any) -> None:
        """Subscribe to reactions on the bot's own messages (1:1 DMs).

        A bot receives UpdateBotMessageReaction for reactions on its own
        messages in private chats -- exactly the owner-DM case. There is no
        high-level Telethon event for it, so filter the raw update.
        """
        if events is None or types is None:
            return

        @self._client.on(events.Raw(types=[types.UpdateBotMessageReaction]))  # type: ignore[untyped-decorator, unused-ignore]
        async def _on_reaction(update: Any) -> None:
            await handler(TelethonReactionAdapter(update))

    async def send_message(
        self,
        *,
        chat_id: int,
        text: str,
        reply_markup: Any | None = None,
        parse_mode: str | None = None,
        **kwargs: Any,
    ) -> Any:
        return await self._client.send_message(
            chat_id,
            text,
            buttons=to_telethon_buttons(reply_markup),
            parse_mode=normalize_parse_mode(parse_mode),
            **_filter_send_kwargs(kwargs),
        )

    async def delete_messages(self, chat_id: int, message_ids: list[int]) -> None:
        await self._client.delete_messages(chat_id, message_ids)

    async def send_chat_action(self, *, chat_id: int, action: str = "typing") -> None:
        """Send a chat-action indicator via Telegram's ``messages.setTyping``.

        Telethon does not expose aiogram's ``send_chat_action`` directly, so
        the migration left the bot with a silently-no-op typing indicator
        across every long-running flow (URL handling, forward summarization,
        batch processing, ...). This wraps ``messages.SetTypingRequest`` and
        accepts the same aiogram-style action strings the rest of the bot
        speaks. Telegram auto-expires the indicator after ~5 s, so callers
        (see ``app/utils/typing_indicator.py``) repeat the call.
        """
        if functions is None or types is None:
            return
        tl_action = _build_typing_tl_action(action)
        if tl_action is None:
            return
        # Resolve the chat id to an InputPeer via Telethon's entity cache so
        # raw TL requests work for any chat the bot has seen.
        try:
            peer = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.debug(
                "send_chat_action_entity_not_found",
                extra={"chat_id": chat_id, "error": str(exc)},
            )
            return
        await self._client(functions.messages.SetTypingRequest(peer=peer, action=tl_action))

    async def react(self, *, chat_id: int, message_id: int, emoji: str) -> None:
        """React to a message with a single emoji (a zero-clutter status ack).

        A bot may set only one reaction per message, so this REPLACES any prior
        reaction (e.g. an 'eyes' processing ack becomes a 'check' on success).
        Best-effort: a disallowed/invalid reaction is swallowed so it never
        breaks the summarize flow.
        """
        if functions is None or types is None:
            return
        try:
            peer = await self._client.get_input_entity(chat_id)
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.debug("react_entity_not_found", extra={"chat_id": chat_id, "error": str(exc)})
            return
        try:
            await self._client(
                functions.messages.SendReactionRequest(
                    peer=peer,
                    msg_id=message_id,
                    reaction=[types.ReactionEmoji(emoticon=emoji)],
                )
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.debug("send_reaction_failed", extra={"chat_id": chat_id, "error": str(exc)})

    async def send_cover_message(self, *, chat_id: int, text: str, url: str) -> Any:
        """Send a message with the source link preview floated ABOVE the text.

        Telegram's high-level send_message cannot set ``invert_media`` (preview
        above text), so build the raw ``messages.sendMessage`` request. The URL
        in the message body triggers Telegram's auto-preview; ``invert_media``
        floats it up as a header/cover card. Best-effort: returns None on error.
        """
        if functions is None or types is None or not url:
            return None
        from telethon.extensions import html as _html

        raw_text, entities = _html.parse(text)
        if url not in raw_text:  # append pristine (outside HTML parsing) so a
            raw_text = f"{raw_text}\n{url}"  # preview is generated to invert
        try:
            peer = await self._client.get_input_entity(chat_id)
            return await self._client(
                functions.messages.SendMessageRequest(
                    peer=peer,
                    message=raw_text,
                    entities=entities or None,
                    invert_media=True,
                )
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.debug("send_cover_failed", extra={"chat_id": chat_id, "error": str(exc)})
            return None

    async def edit_message_text(
        self,
        *,
        chat_id: int,
        message_id: int,
        text: str,
        parse_mode: str | None = None,
        reply_markup: Any | None = None,
        disable_web_page_preview: bool | None = None,
        **_kwargs: Any,
    ) -> Any:
        edit_kwargs: dict[str, Any] = {}
        if disable_web_page_preview is not None:
            edit_kwargs["link_preview"] = not bool(disable_web_page_preview)
        return await self._client.edit_message(
            chat_id,
            message_id,
            text,
            parse_mode=normalize_parse_mode(parse_mode),
            buttons=to_telethon_buttons(reply_markup),
            **edit_kwargs,
        )

    async def set_bot_commands(
        self,
        commands: list[BotCommand],
        *,
        scope: Any | None = None,
        language_code: str | None = None,
        peer: int | None = None,
    ) -> None:
        if functions is None or types is None:
            return
        if peer is not None:
            # Per-chat scope: commands are advertised only in this peer's
            # private chat with the bot. Used to expose admin/debug commands to
            # the owner(s) without leaking them to every user's command menu.
            input_peer = await self._client.get_input_entity(peer)
            scope_obj = types.BotCommandScopePeer(peer=input_peer)
        elif isinstance(scope, BotCommandScopeAllPrivateChats):
            scope_obj = types.BotCommandScopeUsers()
        else:
            scope_obj = types.BotCommandScopeDefault()
        await self._client(
            functions.bots.SetBotCommandsRequest(
                scope=scope_obj,
                lang_code=language_code or "",
                commands=[
                    types.BotCommand(command=cmd.command, description=cmd.description)
                    for cmd in commands
                ],
            )
        )

    async def set_bot_description(self, text: str, *, language_code: str | None = None) -> None:
        await self._set_bot_info(about=text, language_code=language_code)

    async def set_bot_short_description(
        self, text: str, *, language_code: str | None = None
    ) -> None:
        await self._set_bot_info(description=text, language_code=language_code)

    async def _set_bot_info(
        self,
        *,
        about: str | None = None,
        description: str | None = None,
        language_code: str | None = None,
    ) -> None:
        if functions is None:
            return
        try:
            await self._client(
                functions.bots.SetBotInfoRequest(
                    lang_code=language_code or "",
                    about=about,
                    description=description,
                )
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning("set_bot_info_failed", extra={"error": str(exc)})

    async def set_chat_menu_button(self, *, text: str = "Open", url: str | None = None) -> None:
        """Set the bot's persistent menu button to launch the Mini App.

        With a ``url`` the menu button opens an in-Telegram WebView (Mini App).
        Over MTProto the request needs a user; ``InputUserEmpty`` sets the
        global default for all users (the deployment is single-tenant). Without
        a url this is a graceful no-op.
        """
        if functions is None or types is None or not url:
            return
        try:
            await self._client(
                functions.bots.SetBotMenuButtonRequest(
                    user_id=types.InputUserEmpty(),
                    button=types.BotMenuButton(text=text, url=url),
                )
            )
        except Exception as exc:
            raise_if_cancelled(exc)
            logger.warning("set_chat_menu_button_failed", extra={"error": str(exc)})

    async def send_custom_request(self, custom_method: str, params: dict[str, Any]) -> Any:
        if functions is None or types is None:
            msg = "telethon_raw_functions_unavailable"
            raise RuntimeError(msg)
        return await self._client(
            functions.bots.SendCustomRequestRequest(
                custom_method=custom_method,
                params=types.DataJSON(data=json.dumps(params, ensure_ascii=False)),
            )
        )


class TelethonUserClient:
    """Small wrapper around a Telethon user session."""

    def __init__(self, *, session_path: str, api_id: int, api_hash: str) -> None:
        if TelegramClient is None:
            msg = "Telethon is not installed"
            raise RuntimeError(msg)
        self._client = TelegramClient(session_path, api_id, api_hash)

    @property
    def raw(self) -> Any:
        return self._client

    @property
    def is_connected(self) -> bool:
        return bool(self._client.is_connected())

    async def connect(self) -> None:
        await self._client.connect()

    async def start(self, *, interactive: bool = False) -> None:
        if interactive:
            await self._client.start()
            return
        await self._client.connect()
        if not await self._client.is_user_authorized():
            msg = "Telethon user session is not authorized; run /init_session first"
            raise RuntimeError(msg)

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def send_code(self, phone: str) -> Any:
        return await self._client.send_code_request(phone)

    async def sign_in(
        self,
        *,
        phone_number: str | None = None,
        phone_code_hash: str | None = None,
        phone_code: str | None = None,
        password: str | None = None,
    ) -> Any:
        if password is not None:
            return await self._client.sign_in(password=password)
        return await self._client.sign_in(
            phone=phone_number,
            code=phone_code,
            phone_code_hash=phone_code_hash,
        )

    async def check_password(self, password: str) -> Any:
        return await self.sign_in(password=password)

    async def get_me(self) -> Any:
        return await self._client.get_me()

    def get_chat_history(self, channel_username: str) -> Any:
        return self._client.iter_messages(channel_username)

    async def get_chat(self, username: str) -> Any:
        return await self._client.get_entity(username)


__all__ = [
    "TelethonBotClient",
    "TelethonUserClient",
]
