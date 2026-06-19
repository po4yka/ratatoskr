"""OpenAI Whisper speech-to-text client."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx

from app.adapters.transcription.types import TranscriptionResult
from app.core.logging_utils import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)


class OpenAIWhisperSTTError(RuntimeError):
    """Raised when the OpenAI-compatible transcription API rejects a request."""


class OpenAIWhisperSTTClient:
    """Thin HTTP client for OpenAI-compatible audio transcriptions."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "whisper-1",
        base_url: str = "https://api.openai.com/v1",
        timeout_sec: float = 120.0,
    ) -> None:
        if not api_key.strip():
            msg = "STT_API_KEY is required when STT_PROVIDER=openai"
            raise OpenAIWhisperSTTError(msg)
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec

    async def transcribe_file(
        self,
        media_path: Path,
        *,
        language: str | None = None,
        correlation_id: str | None = None,
    ) -> TranscriptionResult:
        """POST ``media_path`` to the OpenAI audio transcriptions endpoint."""
        url = f"{self._base_url}/audio/transcriptions"
        data: dict[str, str] = {"model": self._model, "response_format": "json"}
        if language in {"en", "ru"}:
            data["language"] = language
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            with media_path.open("rb") as fh:
                files = {"file": (media_path.name, fh, _guess_content_type(media_path))}
                async with httpx.AsyncClient(timeout=self._timeout_sec) as client:
                    response = await client.post(url, data=data, files=files, headers=headers)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = _safe_error_body(exc.response)
            logger.warning(
                "openai_stt_http_error",
                extra={
                    "cid": correlation_id,
                    "status_code": exc.response.status_code,
                    "body": body,
                },
            )
            msg = f"OpenAI STT request failed with HTTP {exc.response.status_code}"
            raise OpenAIWhisperSTTError(msg) from exc
        except httpx.HTTPError as exc:
            logger.warning(
                "openai_stt_transport_error",
                extra={"cid": correlation_id, "error": type(exc).__name__},
            )
            msg = f"OpenAI STT transport failed: {type(exc).__name__}"
            raise OpenAIWhisperSTTError(msg) from exc

        payload = response.json()
        if not isinstance(payload, dict):
            msg = "OpenAI STT response was not a JSON object"
            raise OpenAIWhisperSTTError(msg)
        text = str(payload.get("text") or "").strip()
        return TranscriptionResult(
            plain_text=text,
            detected_language=language,
            used_diarization=False,
        )


def _guess_content_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".mp3": "audio/mpeg",
        ".m4a": "audio/mp4",
        ".mp4": "audio/mp4",
        ".ogg": "audio/ogg",
        ".oga": "audio/ogg",
        ".opus": "audio/ogg",
        ".wav": "audio/wav",
        ".webm": "audio/webm",
    }.get(suffix, "application/octet-stream")


def _safe_error_body(response: httpx.Response) -> dict[str, Any] | str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    if not isinstance(payload, dict):
        return str(payload)[:500]
    error = payload.get("error")
    if isinstance(error, dict):
        return {
            key: value
            for key, value in error.items()
            if key in {"message", "type", "code", "param"}
        }
    return payload
