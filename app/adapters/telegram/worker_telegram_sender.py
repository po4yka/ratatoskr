"""Thin httpx-based Telegram Bot API client for the Taskiq worker.

The worker process has no Telethon MTProto session.  All it needs is the
ability to send a new message and later edit it with the final summary.  Both
operations are plain HTTPS POSTs to the Bot API, so a single httpx client is
sufficient and much lighter than a Telethon session.

Retry behaviour:
- 429 Too Many Requests: honours the ``retry_after`` field from the response
  body and sleeps before retrying (one retry only to avoid burning wall time).
- Other 4xx/5xx: logged and re-raised so the caller can record the failure.

Message length:
- Telegram limits messages to 4096 UTF-8 characters.  Text that exceeds the
  limit is truncated and a ``[truncated]`` marker is appended so the reader
  knows the message is incomplete.

Markup:
- Both operations send ``parse_mode=HTML``.  The worker delivers the same
  ``build_card_sections`` markup the bot path produces, so without it Telegram
  renders the tags and escaped entities literally.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from app.core.logging_utils import get_logger

logger = get_logger(__name__)

_TELEGRAM_MAX_CHARS = 4096
_TRUNCATION_MARKER = "\n[truncated]"


def _truncate(text: str) -> str:
    """Truncate *text* to Telegram's 4096-char message limit.

    Cuts on a line boundary when one is available: the card renderer never
    splits an HTML tag or a ``&...;`` entity across lines, so a line-boundary
    cut cannot leave Telegram's parser a broken tag to choke on.

    ponytail: line-boundary cut, not a real HTML-aware splitter. A single line
    longer than the limit still gets a hard cut -- ``_post`` retries without
    ``parse_mode`` if that ever produces markup Telegram rejects. Swap in a
    tag-aware splitter only if a renderer starts emitting multi-line tags.
    """
    if len(text) <= _TELEGRAM_MAX_CHARS:
        return text
    cutoff = _TELEGRAM_MAX_CHARS - len(_TRUNCATION_MARKER)
    head = text[:cutoff]
    boundary = head.rfind("\n")
    if boundary > cutoff // 2:
        head = head[:boundary].rstrip()
    return head + _TRUNCATION_MARKER


class WorkerTelegramSender:
    """Async httpx wrapper for Bot API send/edit operations.

    Instantiate once per worker task invocation (or share a process-level
    instance — httpx.AsyncClient is safe to share across coroutines).

    Args:
        bot_token: Telegram bot token (e.g. ``"123456:ABC-DEF..."``).
        timeout: Per-request timeout in seconds.
    """

    def __init__(self, bot_token: str, *, timeout: float = 30.0) -> None:
        self._token = bot_token
        self._base = f"https://api.telegram.org/bot{bot_token}"
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        cid: str | None = None,
    ) -> int:
        """Send a new message and return its ``message_id``.

        Args:
            chat_id: Telegram chat ID.
            text: Message text (will be truncated to 4096 chars if needed).
            reply_to: Optional message ID to reply to.
            cid: Correlation ID for logging.

        Returns:
            The ``message_id`` of the sent message.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": _truncate(text),
            "parse_mode": "HTML",
        }
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to

        data = await self._post("sendMessage", payload, cid=cid)
        message_id: int = int(data["result"]["message_id"])
        logger.info(
            "worker_telegram_send_ok",
            extra={"cid": cid, "chat_id": chat_id, "message_id": message_id},
        )
        return message_id

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        cid: str | None = None,
    ) -> None:
        """Edit an existing bot message in-place.

        Args:
            chat_id: Telegram chat ID.
            message_id: ID of the message to edit.
            text: New message text (truncated to 4096 chars if needed).
            cid: Correlation ID for logging.
        """
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": _truncate(text),
            "parse_mode": "HTML",
        }
        await self._post("editMessageText", payload, cid=cid)
        logger.info(
            "worker_telegram_edit_ok",
            extra={"cid": cid, "chat_id": chat_id, "message_id": message_id},
        )

    async def _post(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        cid: str | None = None,
    ) -> dict[str, Any]:
        """POST to ``{base}/{method}`` and return the decoded JSON body.

        Retries once on HTTP 429 after sleeping the ``retry_after`` seconds
        specified by Telegram, and once on HTTP 400 without ``parse_mode``.
        """
        url = f"{self._base}/{method}"
        response = await self._client.post(url, json=payload)

        if response.status_code == 429:
            retry_after = _extract_retry_after(response)
            logger.warning(
                "worker_telegram_rate_limited",
                extra={"cid": cid, "method": method, "retry_after": retry_after},
            )
            await asyncio.sleep(retry_after)
            response = await self._client.post(url, json=payload)

        if response.status_code == 400 and payload.get("parse_mode"):
            # Telegram rejects malformed markup ("can't parse entities") with a
            # 400 and delivers nothing. Losing the whole summary is worse than
            # showing it unformatted, so resend as plain text.
            logger.warning(
                "worker_telegram_html_rejected",
                extra={"cid": cid, "method": method, "body": response.text[:500]},
            )
            plain = {k: v for k, v in payload.items() if k != "parse_mode"}
            response = await self._client.post(url, json=plain)

        if not response.is_success:
            logger.error(
                "worker_telegram_request_failed",
                extra={
                    "cid": cid,
                    "method": method,
                    "status": response.status_code,
                    "body": response.text[:500],
                },
            )
            response.raise_for_status()

        data: dict[str, Any] = response.json()
        return data


def _extract_retry_after(response: httpx.Response) -> float:
    """Parse the ``retry_after`` field from a Telegram 429 response body."""
    try:
        body = response.json()
        params = body.get("parameters") or {}
        val = params.get("retry_after")
        if val is not None:
            return max(1.0, float(val))
    except Exception:
        pass
    # Fallback: honour Retry-After header if present.
    header_val = response.headers.get("Retry-After")
    if header_val:
        try:
            return max(1.0, float(header_val))
        except ValueError:
            pass
    return 5.0
