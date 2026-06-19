"""ElevenLabs TTS HTTP client using httpx."""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from app.core.logging_utils import get_logger
from app.observability.metrics import (
    record_tts_audio_bytes,
    record_tts_latency,
    record_tts_request,
)

from .exceptions import (
    ElevenLabsAPIError,
    ElevenLabsQuotaExceededError,
    ElevenLabsRateLimitError,
)

if TYPE_CHECKING:
    from app.config.tts import ElevenLabsConfig

logger = get_logger(__name__)

_BASE_URL = "https://api.elevenlabs.io/v1"
_MAX_RETRIES = 2
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")


class ElevenLabsTTSClient:
    """Async HTTP client for ElevenLabs text-to-speech API."""

    def __init__(self, config: ElevenLabsConfig) -> None:
        self._config = config
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._config.timeout_sec),
                headers={
                    "xi-api-key": self._config.api_key,
                    "Content-Type": "application/json",
                },
            )
        return self._client

    async def synthesize(self, text: str, *, voice_id: str | None = None) -> bytes:
        """Synthesize text to speech audio bytes.

        Args:
            text: Text to synthesize.
            voice_id: Override voice ID (uses config default if None).

        Returns:
            Raw audio bytes (MP3).
        """
        vid = voice_id or self._config.voice_id
        url = f"{_BASE_URL}/text-to-speech/{vid}"
        payload = {
            "text": text,
            "model_id": self._config.model,
            "voice_settings": {
                "stability": self._config.stability,
                "similarity_boost": self._config.similarity_boost,
                "speed": self._config.speed,
            },
            "output_format": self._config.output_format,
        }

        return await self._request_with_retry(url, payload)

    async def synthesize_long(self, text: str) -> bytes:
        """Synthesize long text by chunking at sentence boundaries.

        Uses ``previous_request_ids`` for voice continuity across chunks
        and concatenates the resulting MP3 byte streams.
        """
        chunks = self._chunk_text(text)
        if len(chunks) <= 1:
            return await self.synthesize(text)

        audio_parts: list[bytes] = []
        previous_request_ids: list[str] = []

        for chunk in chunks:
            vid = self._config.voice_id
            url = f"{_BASE_URL}/text-to-speech/{vid}"
            payload: dict[str, Any] = {
                "text": chunk,
                "model_id": self._config.model,
                "voice_settings": {
                    "stability": self._config.stability,
                    "similarity_boost": self._config.similarity_boost,
                    "speed": self._config.speed,
                },
                "output_format": self._config.output_format,
            }
            if previous_request_ids:
                payload["previous_request_ids"] = previous_request_ids[-3:]

            data = await self._request_with_retry(url, payload)
            audio_parts.append(data)

        return b"".join(audio_parts)

    def _chunk_text(self, text: str) -> list[str]:
        """Split text at sentence boundaries respecting max_chars_per_request."""
        max_chars = self._config.max_chars_per_request
        if len(text) <= max_chars:
            return [text]

        sentences = _SENTENCE_BOUNDARY.split(text)
        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        for sentence in sentences:
            sentence_len = len(sentence)
            if current_len + sentence_len > max_chars and current:
                chunks.append(" ".join(current))
                current = []
                current_len = 0
            current.append(sentence)
            current_len += sentence_len + 1  # +1 for space

        if current:
            chunks.append(" ".join(current))

        return chunks

    async def _request_with_retry(self, url: str, payload: dict[str, Any]) -> bytes:
        """Execute POST request with retry on transient errors."""
        client = self._get_client()
        last_exc: Exception | None = None
        started = time.monotonic()

        for attempt in range(_MAX_RETRIES + 1):
            try:
                response = await client.post(url, json=payload)
                if response.status_code == 200:
                    content = response.content
                    record_tts_request("success")
                    record_tts_audio_bytes(len(content))
                    record_tts_latency(time.monotonic() - started)
                    return content

                self._handle_error_response(response)

            except (ElevenLabsRateLimitError, ElevenLabsAPIError) as exc:
                last_exc = exc
                if exc.status_code not in _RETRYABLE_STATUS_CODES:
                    self._record_terminal_error(exc, started)
                    raise
                if attempt < _MAX_RETRIES:
                    delay = 2 ** (attempt + 1)
                    record_tts_request("retry")
                    logger.warning(
                        "elevenlabs_retry",
                        extra={
                            "attempt": attempt + 1,
                            "status_code": exc.status_code,
                            "delay_sec": delay,
                        },
                    )
                    await asyncio.sleep(delay)
                else:
                    self._record_terminal_error(exc, started)
                    raise

            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    delay = 2 ** (attempt + 1)
                    record_tts_request("retry")
                    logger.warning(
                        "elevenlabs_http_error_retry",
                        extra={"attempt": attempt + 1, "error": str(exc), "delay_sec": delay},
                    )
                    await asyncio.sleep(delay)
                else:
                    msg = f"ElevenLabs request failed after {_MAX_RETRIES + 1} attempts: {exc}"
                    record_tts_request("timeout" if isinstance(exc, httpx.TimeoutException) else "http_error")
                    record_tts_latency(time.monotonic() - started)
                    raise ElevenLabsAPIError(msg) from exc

        # Should not reach here, but satisfy type checker
        msg = "ElevenLabs request failed"
        record_tts_request("http_error")
        record_tts_latency(time.monotonic() - started)
        raise ElevenLabsAPIError(msg) from last_exc

    @staticmethod
    def _record_terminal_error(exc: ElevenLabsAPIError, started: float) -> None:
        outcome = "quota_exceeded" if isinstance(exc, ElevenLabsQuotaExceededError) else "http_error"
        record_tts_request(outcome)
        record_tts_latency(time.monotonic() - started)

    @staticmethod
    def _handle_error_response(response: httpx.Response) -> None:
        """Raise appropriate exception for error HTTP responses."""
        status = response.status_code
        try:
            body = response.json()
            detail = body.get("detail", {})
            message = detail.get("message", "") if isinstance(detail, dict) else str(detail)
        except Exception:
            message = response.text[:200]

        if status == 429:
            raise ElevenLabsRateLimitError(f"Rate limited: {message}", status_code=status)
        if status == 401:
            raise ElevenLabsAPIError("Invalid API key", status_code=status)
        if "quota" in message.lower() or "characters" in message.lower():
            raise ElevenLabsQuotaExceededError(f"Quota exceeded: {message}", status_code=status)
        raise ElevenLabsAPIError(f"ElevenLabs API error ({status}): {message}", status_code=status)

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
