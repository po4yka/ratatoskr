"""Telegram sendMessageDraft transport with per-request fallback handling."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.core.async_utils import raise_if_cancelled
from app.core.logging_utils import get_logger
from app.observability.metrics import record_circuit_breaker_state, record_draft_stream_event

logger = get_logger(__name__)


@dataclass(frozen=True)
class DraftStreamSettings:
    """Configuration for draft streaming throttling and limits."""

    enabled: bool = True
    min_interval_ms: int = 700
    min_delta_chars: int = 40
    max_chars: int = 3500


_STALE_ENTRY_THRESHOLD_SEC: float = 1800.0  # 30 minutes


@dataclass
class DraftStreamState:
    """Mutable state for a single request-level draft stream."""

    last_text: str = ""
    last_sent_monotonic: float = 0.0
    fallback: bool = False
    fallback_reason: str | None = None
    fallback_until: float = 0.0
    consecutive_failures: int = 0
    _created_at: float = 0.0

    def __post_init__(self) -> None:
        if self._created_at == 0.0:
            self._created_at = time.time()


@dataclass(frozen=True)
class DraftSendResult:
    """Result of a draft update attempt."""

    ok: bool
    sent: bool
    fallback: bool
    reason: str | None = None


_TRANSPORT_CIRCUIT_BREAKER_THRESHOLD: int = 3
_CIRCUIT_RESET_TIMEOUT_SEC: float = 300.0


class DraftStreamSender:
    """Sends Telegram draft updates via raw SendCustomRequest transport."""

    def __init__(
        self,
        *,
        telegram_client: Any,
        settings: DraftStreamSettings,
    ) -> None:
        self._telegram_client = telegram_client
        self._settings = settings
        self._states: dict[tuple[int, int], DraftStreamState] = {}
        self._transport_disabled: bool = False
        self._transport_disabled_since: float = 0.0
        self._transport_consecutive_failures: int = 0
        self._transport_half_open: bool = False

    def set_telegram_client(self, telegram_client: Any) -> None:
        """Update Telegram client reference (used by ResponseFormatter rewiring)."""
        self._telegram_client = telegram_client

    @property
    def enabled(self) -> bool:
        return bool(self._settings.enabled)

    def clear(self, message: Any) -> None:
        """Clear tracked draft state for the message key."""
        key = self.request_key(message)
        if key is not None:
            self._states.pop(key, None)

    def request_key(self, message: Any) -> tuple[int, int] | None:
        """Build request key from incoming message for per-request stream state."""
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        trigger_message_id = getattr(message, "id", None) or getattr(message, "message_id", None)
        if chat_id is None or trigger_message_id is None:
            return None
        return int(chat_id), int(trigger_message_id)

    def _evict_stale(self) -> None:
        """Remove state entries older than the staleness threshold."""
        now = time.time()
        stale_keys = [
            key
            for key, state in self._states.items()
            if now - state._created_at > _STALE_ENTRY_THRESHOLD_SEC
        ]
        for key in stale_keys:
            del self._states[key]
        if stale_keys:
            logger.debug("draft_evict_stale", extra={"evicted": len(stale_keys)})

    async def send_update(
        self,
        message: Any,
        text: str,
        *,
        message_thread_id: int | None = None,
        force: bool = False,
    ) -> DraftSendResult:
        """Send one draft update with strict fallback-once semantics."""
        self._evict_stale()

        if not self._settings.enabled:
            return DraftSendResult(ok=False, sent=False, fallback=True, reason="disabled")

        if self._transport_disabled:
            elapsed = time.time() - self._transport_disabled_since
            if elapsed < _CIRCUIT_RESET_TIMEOUT_SEC:
                return DraftSendResult(
                    ok=False, sent=False, fallback=True, reason="transport_circuit_open"
                )
            self._transport_disabled = False
            self._transport_consecutive_failures = 0
            self._transport_half_open = True
            logger.info(
                "draft_transport_circuit_half_open",
                extra={"disabled_for_sec": round(elapsed)},
            )
            record_circuit_breaker_state("draft_stream", "half_open")

        key = self.request_key(message)
        if key is None:
            return DraftSendResult(
                ok=False, sent=False, fallback=True, reason="missing_request_key"
            )

        state = self._states.setdefault(key, DraftStreamState())
        if state.fallback or (state.fallback_until > 0 and time.time() < state.fallback_until):
            return DraftSendResult(
                ok=False,
                sent=False,
                fallback=True,
                reason=state.fallback_reason or "draft_cooldown",
            )

        normalized = self._normalize_text(text)
        if not normalized:
            return DraftSendResult(ok=True, sent=False, fallback=False, reason="empty_text")

        if not force and self._is_throttled(state, normalized):
            return DraftSendResult(ok=True, sent=False, fallback=False, reason="throttled")

        chat_id, _ = key
        params: dict[str, Any] = {"chat_id": chat_id, "text": normalized}
        if message_thread_id is not None:
            params["message_thread_id"] = int(message_thread_id)

        record_draft_stream_event("draft_send_attempt")
        logger.debug(
            "draft_send_attempt",
            extra={
                "chat_id": chat_id,
                "message_thread_id": message_thread_id,
                "length": len(normalized),
            },
        )

        try:
            await self._send_custom_request(params)
            state.last_text = normalized
            state.last_sent_monotonic = time.monotonic()
            state.consecutive_failures = 0
            state.fallback_until = 0.0
            self._transport_consecutive_failures = 0
            self._transport_disabled_since = 0.0
            self._transport_half_open = False
            record_circuit_breaker_state("draft_stream", "closed")
            record_draft_stream_event("draft_send_success")
            logger.debug(
                "draft_send_success",
                extra={"chat_id": chat_id, "length": len(normalized)},
            )
            return DraftSendResult(ok=True, sent=True, fallback=False)
        except Exception as exc:
            raise_if_cancelled(exc)
            reason = self._classify_failure_reason(exc)
            state.consecutive_failures += 1
            state.fallback_reason = reason
            if state.consecutive_failures >= 3:
                state.fallback = True
            else:
                state.fallback_until = time.time() + 10

            self._transport_consecutive_failures += 1
            reopen = self._transport_half_open or (
                self._transport_consecutive_failures >= _TRANSPORT_CIRCUIT_BREAKER_THRESHOLD
            )
            if not self._transport_disabled and reopen:
                self._transport_disabled = True
                self._transport_disabled_since = time.time()
                self._transport_half_open = False
                logger.warning(
                    "draft_transport_circuit_open",
                    extra={
                        "failures": self._transport_consecutive_failures,
                        "last_error": str(exc),
                    },
                )
                record_circuit_breaker_state("draft_stream", "open")

            record_draft_stream_event("draft_send_fallback")
            if reason == "policy_reject":
                record_draft_stream_event("draft_send_policy_reject")
            logger.debug(
                "draft_send_fallback",
                extra={
                    "chat_id": chat_id,
                    "reason": reason,
                    "error": str(exc),
                },
            )
            return DraftSendResult(ok=False, sent=False, fallback=True, reason=reason)

    def _normalize_text(self, text: str) -> str:
        normalized = (text or "").strip()
        if not normalized:
            return ""
        if len(normalized) > self._settings.max_chars:
            return normalized[: self._settings.max_chars - 1].rstrip() + "…"
        return normalized

    def _is_throttled(self, state: DraftStreamState, text: str) -> bool:
        if state.last_sent_monotonic <= 0:
            return False
        now = time.monotonic()
        elapsed_ms = (now - state.last_sent_monotonic) * 1000.0
        chars_delta = abs(len(text) - len(state.last_text))
        return text == state.last_text or (
            elapsed_ms < self._settings.min_interval_ms
            and chars_delta < self._settings.min_delta_chars
        )

    async def _send_custom_request(self, params: dict[str, Any]) -> None:
        # `TelegramClient.client` is the TelethonBotClient wrapper (the raw
        # Telethon client is `.raw`), and the wrapper's transport method is
        # `send_custom_request`. It has no `invoke` -- that name belongs to the
        # Bot-API-era client this guard was written against -- so requiring
        # `invoke` made this method raise unconditionally and draft streaming
        # was dead for every send while the feature flag defaulted to on.
        # Gate on the one attribute actually called.
        client = getattr(self._telegram_client, "client", None)
        if client is None or not hasattr(client, "send_custom_request"):
            msg = "telegram_client_custom_request_unavailable"
            raise RuntimeError(msg)

        await client.send_custom_request("sendMessageDraft", params)

    @staticmethod
    def _classify_failure_reason(exc: Exception) -> str:
        error_text = str(exc).lower()
        policy_markers = (
            "not enough stars",
            "stars",
            "fee",
            "payment",
            "premium",
            "policy",
            "forbidden",
        )
        unsupported_markers = (
            "unknown method",
            "method not found",
            "sendmessagedraft",
            "not implemented",
        )
        if any(marker in error_text for marker in policy_markers):
            return "policy_reject"
        if any(marker in error_text for marker in unsupported_markers):
            return "unsupported"
        return "transport_error"
