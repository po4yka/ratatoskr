"""Protocol adapters: Telethon event objects → bot handler interface.

TelethonMessageAdapter, TelethonCallbackQueryAdapter, TelethonReactionAdapter
each wrap a raw Telethon event and expose the aiogram-style API expected by
command handlers and the message router.
"""

from __future__ import annotations

from typing import Any, cast

from app.adapters.telegram.compat_entities import _peer_to_id, _translate_entities
from app.adapters.telegram.compat_keyboards import _filter_send_kwargs, to_telethon_buttons
from app.adapters.telegram.compat_telethon import types
from app.adapters.telegram.compat_types import _Entity, _Object, normalize_parse_mode


class TelethonMessageAdapter:
    """Expose a Telethon message event with the methods used by handlers."""

    def __init__(self, event: Any, bot: Any) -> None:
        self._event = event
        self._bot = bot
        self._message = event.message
        self._client = bot

    def __getattr__(self, name: str) -> Any:
        return getattr(self._message, name)

    @property
    def id(self) -> int:
        return int(getattr(self._message, "id", 0))

    @property
    def chat(self) -> Any:
        chat_id = getattr(self._message, "chat_id", None)
        return getattr(self._message, "chat", None) or _Object(id=chat_id)

    @property
    def from_user(self) -> Any:
        sender = getattr(self._message, "sender", None) or getattr(self._event, "sender", None)
        if sender is None:
            return None
        return _Object(
            id=getattr(sender, "id", None),
            first_name=getattr(sender, "first_name", None),
            username=getattr(sender, "username", None),
            is_bot=getattr(sender, "bot", False),
        )

    @property
    def text(self) -> str | None:
        return cast(
            "str | None",
            getattr(self._message, "raw_text", None) or getattr(self._message, "text", None),
        )

    @property
    def caption(self) -> str | None:
        return None

    @property
    def entities(self) -> list[_Entity]:
        """Message entities translated to the aiogram-style shape.

        Without this, ``__getattr__`` delegated ``.entities`` to the raw
        Telethon message, whose entity objects carry no ``.type`` -- so every
        entity (including hyperlinked ``text_link`` words) was parsed as a
        ``mention`` and ``TelegramMessage.get_urls()`` always returned ``[]``.
        """
        return _translate_entities(getattr(self._message, "entities", None))

    @property
    def caption_entities(self) -> list[Any]:
        """Telethon keeps caption text + entities on the message itself, so the
        aiogram-style separate caption-entity list is always empty here."""
        return []

    # -- Forward metadata -------------------------------------------------
    # Telethon exposes forwards via the raw ``fwd_from`` (``MessageFwdHeader``)
    # and the entity-enriched ``message.forward`` helper. The rest of the bot
    # (router, ``TelegramMessage`` parser, forward processor) speaks the
    # aiogram-style ``forward_*`` vocabulary, so translate it here. Without
    # these properties ``__getattr__`` delegated the names to the raw Telethon
    # message, which has no such attributes, so every forward was misread as
    # plain text and answered with the generic fallback prompt.

    @property
    def _fwd_header(self) -> Any:
        """Raw Telethon ``MessageFwdHeader`` for this message, or None."""
        return getattr(self._message, "fwd_from", None)

    @property
    def _fwd(self) -> Any:
        """Telethon's entity-enriched ``Forward`` helper, or None."""
        try:
            return getattr(self._message, "forward", None)
        except Exception:  # pragma: no cover - defensive: never block routing
            return None

    @property
    def forward_date(self) -> Any:
        header = self._fwd_header
        return getattr(header, "date", None) if header is not None else None

    @property
    def forward_sender_name(self) -> str | None:
        """Origin name for forwards where the sender hid their account."""
        header = self._fwd_header
        return (
            cast("str | None", getattr(header, "from_name", None)) if header is not None else None
        )

    @property
    def forward_signature(self) -> str | None:
        header = self._fwd_header
        return (
            cast("str | None", getattr(header, "post_author", None)) if header is not None else None
        )

    @property
    def forward_from_message_id(self) -> int | None:
        """Message id of the original post in the source channel/chat."""
        header = self._fwd_header
        if header is None:
            return None
        raw = getattr(header, "channel_post", None) or getattr(header, "saved_from_msg_id", None)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def forward_from_chat(self) -> Any:
        """Channel/group the message was forwarded from, aiogram-style."""
        header = self._fwd_header
        if header is None:
            return None
        from_id = getattr(header, "from_id", None)
        if from_id is None:
            return None
        if types is not None and isinstance(from_id, types.PeerUser):
            return None  # user forward -- see ``forward_from``
        chat_id = _peer_to_id(from_id)
        if chat_id is None:
            return None
        entity = getattr(self._fwd, "chat", None)
        chat_type = "channel"
        if entity is not None:
            if getattr(entity, "megagroup", False):
                chat_type = "supergroup"
            elif getattr(entity, "broadcast", False):
                chat_type = "channel"
            else:
                chat_type = "group"
        return _Object(
            id=chat_id,
            username=getattr(entity, "username", None),
            title=getattr(entity, "title", None),
            type=chat_type,
        )

    @property
    def forward_from(self) -> Any:
        """User the message was forwarded from, aiogram-style."""
        header = self._fwd_header
        if header is None:
            return None
        from_id = getattr(header, "from_id", None)
        if from_id is None or types is None or not isinstance(from_id, types.PeerUser):
            return None
        user_id = _peer_to_id(from_id)
        if user_id is None:
            return None
        entity = getattr(self._fwd, "sender", None)
        return _Object(
            id=user_id,
            first_name=getattr(entity, "first_name", None) or "",
            username=getattr(entity, "username", None),
            is_bot=bool(getattr(entity, "bot", False)),
        )

    async def reply_text(self, text: str, **kwargs: Any) -> Any:
        return await self._event.reply(
            text,
            buttons=to_telethon_buttons(kwargs.pop("reply_markup", None)),
            parse_mode=normalize_parse_mode(kwargs.pop("parse_mode", None)),
            **_filter_send_kwargs(kwargs),
        )

    async def reply_document(
        self, document: Any, *, caption: str | None = None, **kwargs: Any
    ) -> Any:
        return await self._client.raw.send_file(
            self.chat.id,
            document,
            caption=caption,
            buttons=to_telethon_buttons(kwargs.pop("reply_markup", None)),
        )

    async def download(self, *, file_name: str | None = None) -> str | None:
        return cast("str | None", await self._bot.raw.download_media(self._message, file=file_name))


class TelethonCallbackQueryAdapter:
    """Expose callback query fields used by the callback handler."""

    def __init__(self, event: Any, bot: Any) -> None:
        self._event = event
        self._bot = bot
        data = getattr(event, "data", b"")
        self.data = data.decode("utf-8") if isinstance(data, bytes) else str(data)
        self.message = (
            TelethonMessageAdapter(event, bot) if getattr(event, "message", None) else None
        )
        self.from_user = _Object(id=getattr(event, "sender_id", None))

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        await self._event.answer(text or "", alert=show_alert)


class TelethonReactionAdapter:
    """Expose the fields of an ``UpdateBotMessageReaction`` used by handlers."""

    def __init__(self, update: Any) -> None:
        self._update = update

    @property
    def chat_id(self) -> int | None:
        return _peer_to_id(getattr(self._update, "peer", None))

    @property
    def message_id(self) -> int | None:
        raw = getattr(self._update, "msg_id", None)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def emoji(self) -> str | None:
        """First standard-emoji reaction now present, or None (custom/removed)."""
        for reaction in getattr(self._update, "new_reactions", None) or []:
            emoticon = getattr(reaction, "emoticon", None)
            if emoticon:
                return str(emoticon)
        return None


__all__ = [
    "TelethonCallbackQueryAdapter",
    "TelethonMessageAdapter",
    "TelethonReactionAdapter",
]
